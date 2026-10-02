"""The watch timer: pure, deterministic accounting of viewing time (WT-1..WT-8).

The timer is driven by the caller with explicit ``now`` values. Between two
calls the activity is constant, so time is accrued piecewise up to the next
*event* (daily reset, allowance hitting zero, maximum session length, end of
the session break). This keeps results exact however sparse the ticks are:
the grace deadline is anchored at the moment a limit was hit, not at the tick
that noticed it.

A *viewing session* (WT-3) is the stretch of watching that the maximum
session length applies to. It is named so to avoid confusion with the
per-episode ``watch_session`` history rows.

Profiles (PR-2, PR-4): the *watchers* are the profiles of the current pick.
Only they accrue time, and the limits that stop playback are theirs. Every
profile has its own viewing session and its own exhaustion (A-13): a profile
that stops watching starts its session break at that moment. A group may start
only if every member can.
"""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta

from .day import day_for, next_reset_after
from .models import (
    Action,
    Activity,
    CountingMode,
    DayUsage,
    Decision,
    ProfilePolicy,
    ProfileStatus,
    TimerSettings,
    TimeUpReason,
)

# Tolerance for float/microsecond rounding when comparing seconds.
_EPS = 1e-3
_SNAPSHOT_VERSION = 2


@dataclass(frozen=True)
class _Exhaustion:
    at: datetime  # the exact moment the limit was hit
    grace_deadline: datetime | None  # None: hit while stopped, so no grace

    @property
    def stop_at(self) -> datetime:
        return self.grace_deadline or self.at


@dataclass
class _Session:
    """One profile's viewing session (WT-3)."""

    start: datetime
    inactive_since: datetime | None = None  # None while the profile is watching and playing


class WatchTimer:
    def __init__(
        self,
        settings: TimerSettings,
        policies: list[ProfilePolicy],
        usages: list[DayUsage],
        now: datetime,
        snapshot: dict | None = None,
    ) -> None:
        self._settings = settings
        self._policies = {p.profile_id: p for p in policies}
        self._t = now  # everything is accounted up to here
        self._day = self._day_of(now)
        self._next_reset = self._reset_after(now)
        self._usages = {pid: DayUsage(pid, self._day) for pid in self._policies}
        for u in usages:  # rows for other days or unknown profiles are ignored
            if u.day == self._day and u.profile_id in self._usages:
                self._usages[u.profile_id] = replace(u)
        self._activity = Activity.STOPPED
        self._watchers_all = True  # no pick yet (today): every profile counts, as in v1
        self._watchers: set[int] = set()
        self._sessions: dict[int, _Session] = {}
        self._exhausted: dict[int, dict[TimeUpReason, _Exhaustion]] = {}  # ALLOWANCE / SESSION_MAX only
        self._dirty: set[int] = set()
        self._dirty_prev: list[DayUsage] = []  # final rows of days rolled over
        self._counted_s = 0.0
        if snapshot is not None:
            self._restore(snapshot)
        self._check(now)

    # -- public API -------------------------------------------------------

    @property
    def activity(self) -> Activity:
        return self._activity

    def tick(self, now: datetime) -> Decision:
        return self._decide(self._sync(now))

    def decision(self, now: datetime) -> Decision:
        return self.tick(now)

    def set_activity(self, now: datetime, activity: Activity) -> Decision:
        now = self._sync(now)  # accrue under the old activity first
        old, self._activity = self._activity, activity
        if activity is Activity.PLAYING:
            for pid in self.watchers:
                session = self._sessions.get(pid)
                if session is None:  # WT-3: first PLAYING starts a session
                    self._sessions[pid] = _Session(now)
                else:
                    session.inactive_since = None
        elif old is Activity.PLAYING:
            for pid in self.watchers:
                if pid in self._sessions:
                    self._sessions[pid].inactive_since = now  # WT-3: the session break starts counting
        self._check(now)
        return self._decide(now)

    @property
    def watchers(self) -> frozenset[int]:
        """The profiles watching the current pick (PR-2). Only they accrue time (PR-4)."""
        return frozenset(self._policies) if self._watchers_all else frozenset(self._watchers)

    def set_watchers(self, now: datetime, profile_ids: Collection[int]) -> Decision:
        """Make ``profile_ids`` the watchers without checking whether they may start;
        used when the cast service re-attaches to playback after a restart."""
        ids = self._resolve(profile_ids)
        now = self._sync(now)
        self._attach(now, ids, False)
        self._check(now)
        return self._decide(now)

    def group_decision(self, now: datetime, profile_ids: Collection[int]) -> Decision:
        """Would a pick by this group start now? No side effects. A group may start only if
        every member has time left and none is blocked or past its session max (PR-4)."""
        ids = self._resolve(profile_ids)
        return self._decide_for(ids, self._sync(now))

    def profile_status(self, now: datetime, profile_id: int) -> ProfileStatus:
        """One profile on its own, for the who's-watching screen and the dashboard."""
        if profile_id not in self._policies:
            raise KeyError(profile_id)
        now = self._sync(now)
        d = self._decide_for(frozenset({profile_id}), now)
        session = self._sessions.get(profile_id)
        return ProfileStatus(
            profile_id=profile_id,
            remaining_s=d.remaining_s,
            can_start=d.can_start,
            reason=d.reason,
            session_elapsed_s=None if session is None else (now - session.start).total_seconds(),
            watching=profile_id in self.watchers,
        )

    def on_pick(self, now: datetime, profile_ids: Collection[int] | None = None) -> Decision:
        """A pick by ``profile_ids`` (None: every profile, the v1 behaviour). If the group may
        start, it becomes the watchers and each member's viewing session starts or extends."""
        ids = self._resolve(profile_ids)
        now = self._sync(now)
        decision = self._decide_for(ids, now)
        if not decision.can_start:
            return decision
        self._attach(now, ids, True, everyone=profile_ids is None)
        return self._decide(now)

    def _resolve(self, profile_ids: Collection[int] | None) -> frozenset[int]:
        ids = frozenset(self._policies) if profile_ids is None else frozenset(profile_ids)
        if not ids:
            raise ValueError("a group needs at least one profile")
        unknown = ids - self._policies.keys()
        if unknown:
            raise KeyError(sorted(unknown)[0])
        return ids

    def _attach(self, now: datetime, ids: frozenset[int], pick: bool, everyone: bool = False) -> None:
        """Make ``ids`` the watchers. Members that stop watching start their session break now;
        a pick starts or extends the members' sessions (WT-3)."""
        playing = self._activity is Activity.PLAYING
        for pid in self.watchers - ids:
            session = self._sessions.get(pid)
            if session is not None and session.inactive_since is None:
                session.inactive_since = now
        for pid in ids:
            session = self._sessions.get(pid)
            if session is None:
                if pick or playing:
                    self._sessions[pid] = _Session(now, None if playing else now)
            elif playing:
                session.inactive_since = None
            elif pick:
                session.inactive_since = now
        self._watchers_all = everyone
        self._watchers = set(ids)

    @property
    def next_reset(self) -> datetime:
        """The next daily reset (WT-1), kept in step with the settings."""
        return self._next_reset

    def set_policies(self, settings: TimerSettings, policies: list[ProfilePolicy], now: datetime) -> None:
        now = self._sync(now)
        self._settings = settings
        self._policies = {p.profile_id: p for p in policies}
        if self._day_of(now) != self._day:  # reset time or zone moved the day
            self._rollover(self._day_of(now))
        self._usages = {pid: self._usages.get(pid) or DayUsage(pid, self._day) for pid in self._policies}
        self._dirty &= self._usages.keys()
        for state in (self._sessions, self._exhausted):  # deleted profiles leave the timer
            for pid in [pid for pid in state if pid not in self._policies]:
                del state[pid]
        self._watchers &= self._policies.keys()
        if not self._watchers:
            self._watchers_all = True
        self._next_reset = self._reset_after(now)
        self._check(now)

    def add_extra(self, now: datetime, profile_id: int, seconds: float) -> Decision:
        """WT-7: extra time for today, cumulative."""
        return self._override(now, profile_id, lambda u: setattr(u, "extra_s", u.extra_s + seconds))

    def set_unlimited(self, now: datetime, profile_id: int, value: bool) -> Decision:
        """WT-7: unlimited for today; lifts the allowance and this profile's session max."""
        return self._override(now, profile_id, lambda u: setattr(u, "unlimited", value))

    def set_blocked(self, now: datetime, profile_id: int, value: bool) -> Decision:
        """WT-7: block viewing for today; stops playback immediately, no grace."""
        return self._override(now, profile_id, lambda u: setattr(u, "blocked", value))

    def usage(self, profile_id: int) -> DayUsage:
        return replace(self._usages[profile_id])

    def pop_dirty_usage(self) -> list[DayUsage]:
        rows = self._dirty_prev + [replace(self._usages[pid]) for pid in sorted(self._dirty)]
        self._dirty_prev, self._dirty = [], set()
        return rows

    def pop_counted_s(self) -> float:
        counted, self._counted_s = self._counted_s, 0.0
        return counted

    def snapshot(self) -> dict:
        """State as of the last call, for WT-8. Usage is persisted separately."""
        sessions = {}
        for pid, session in self._sessions.items():
            watching_now = self._activity is Activity.PLAYING and pid in self.watchers
            last_active = self._t if watching_now else session.inactive_since
            sessions[str(pid)] = {"started_at": _iso(session.start), "last_active_at": _iso(last_active)}
        return {
            "version": _SNAPSHOT_VERSION,
            "as_of": self._t.isoformat(),
            "day": self._day.isoformat(),
            "activity": self._activity.value,
            "watchers": None if self._watchers_all else sorted(self._watchers),
            "sessions": sessions,
            "exhausted": {
                str(pid): {
                    reason.value: {"at": e.at.isoformat(), "grace_deadline": _iso(e.grace_deadline)}
                    for reason, e in exhausted.items()
                }
                for pid, exhausted in self._exhausted.items()
                if exhausted
            },
        }

    # -- internals --------------------------------------------------------

    def _restore(self, snap: dict) -> None:
        """Restored timers start STOPPED; time since the snapshot is not counted
        (conservative: we cannot know whether anything played meanwhile)."""
        same_day = date.fromisoformat(snap["day"]) == self._day
        watchers = None
        if snap.get("version", 1) < 2:  # v1: one timer-wide session and exhaustion, every profile watched
            started = snap["session_started_at"]
            v1_session = {"started_at": started, "last_active_at": snap["last_active_at"]}
            sessions = {pid: v1_session for pid in self._policies} if started else {}
            exhausted = {pid: snap["exhausted"] for pid in self._policies}
        else:
            sessions = {int(k): v for k, v in snap["sessions"].items()}
            exhausted = {int(k): v for k, v in snap["exhausted"].items()}
            watchers = snap["watchers"]
        if watchers is not None:
            self._watchers = {int(pid) for pid in watchers} & self._policies.keys()
            self._watchers_all = not self._watchers
        for pid, v in sessions.items():
            if pid in self._policies:
                self._sessions[pid] = _Session(_parse(v["started_at"]), _parse(v["last_active_at"]))
        for pid, per_reason in exhausted.items():
            if pid not in self._policies:
                continue
            for reason, e in per_reason.items():
                reason = TimeUpReason(reason)
                if reason is TimeUpReason.ALLOWANCE and not same_day:
                    continue  # WT-1: a new day clears allowance exhaustion
                self._exhausted.setdefault(pid, {})[reason] = _Exhaustion(
                    _parse(e["at"]), _parse(e["grace_deadline"])
                )
        # Session end (break elapsed) is applied by the _check() that follows.

    def _override(self, now: datetime, profile_id: int, change) -> Decision:
        usage = self._usages[profile_id]  # KeyError for unknown profiles
        now = self._sync(now)
        change(usage)
        self._dirty.add(profile_id)
        self._check(now)
        return self._decide(now)

    def _sync(self, now: datetime) -> datetime:
        """Accrue up to ``now`` event by event. Time never runs backwards."""
        now = max(now, self._t)
        while self._t < now:
            t = self._t
            step = min((e for e in self._events(t) if e > t), default=now)
            step = min(step, now)
            self._accrue((step - t).total_seconds())
            self._t = step
            self._check(step)
        self._check(now)
        return now

    def _events(self, t: datetime):
        """Moments after ``t`` at which the state may change, given constant activity."""
        yield self._next_reset
        for pid in self.watchers:
            p = self._policies[pid]
            if TimeUpReason.ALLOWANCE not in self._exhausted.get(pid, {}):
                remaining = self._usages[pid].remaining_s(p.allowance_s)
                if remaining is not None and self._counts(p):
                    yield t + timedelta(seconds=remaining)
        for pid, session in self._sessions.items():
            max_s = self._max_session_s(pid)
            if max_s is not None and TimeUpReason.SESSION_MAX not in self._exhausted.get(pid, {}):
                yield session.start + timedelta(seconds=max_s)
            if session.inactive_since is not None:
                yield session.inactive_since + timedelta(seconds=self._settings.session_break_s)

    def _counts(self, p: ProfilePolicy) -> bool:
        """WT-2: ignore_pauses counts playing only; wall_clock also counts paused."""
        return self._activity is Activity.PLAYING or (
            self._activity is Activity.PAUSED and p.mode == CountingMode.WALL_CLOCK
        )

    def _accrue(self, seconds: float) -> None:
        counted = False
        for pid in self.watchers:  # PR-4 / A-1: every watching profile pays, and only they
            if self._counts(self._policies[pid]):
                self._usages[pid].used_s += seconds
                self._dirty.add(pid)
                counted = True
        if counted:
            self._counted_s += seconds

    def _check(self, at: datetime) -> None:
        """Apply every state transition due at ``at``."""
        while at >= self._next_reset:  # WT-1
            self._rollover(self._day_of(self._next_reset))
            self._next_reset = self._reset_after(self._next_reset)

        break_s = timedelta(seconds=self._settings.session_break_s)
        for pid in [
            pid
            for pid, s in self._sessions.items()  # WT-3: a full break without playing ends the session
            if s.inactive_since is not None and at >= s.inactive_since + break_s
        ]:
            del self._sessions[pid]

        # WT-4/WT-5: grace only when media is loaded at the moment the limit is hit.
        loaded = self._activity is not Activity.STOPPED
        hit = _Exhaustion(at, at + timedelta(seconds=self._settings.grace_cap_s) if loaded else None)

        for pid, p in self._policies.items():
            exhausted = self._exhausted.setdefault(pid, {})
            remaining = self._usages[pid].remaining_s(p.allowance_s)
            if remaining is not None and remaining <= _EPS:
                exhausted.setdefault(TimeUpReason.ALLOWANCE, hit)
            else:  # WT-7: extra/unlimited lift allowance exhaustion immediately
                exhausted.pop(TimeUpReason.ALLOWANCE, None)

            max_s, elapsed = self._max_session_s(pid), self._elapsed_s(pid, at)
            if max_s is not None and elapsed is not None and elapsed >= max_s - _EPS:
                exhausted.setdefault(TimeUpReason.SESSION_MAX, hit)
            else:  # session ended, or the profile went unlimited
                exhausted.pop(TimeUpReason.SESSION_MAX, None)

    def _rollover(self, new_day: date) -> None:
        """WT-1/WT-7: a new day gets fresh usage (no overrides); the previous
        day's changed rows are kept for pop_dirty_usage()."""
        self._dirty_prev += [replace(self._usages[pid]) for pid in sorted(self._dirty)]
        self._dirty = set()
        self._day = new_day
        self._usages = {pid: DayUsage(pid, new_day) for pid in self._policies}
        for exhausted in self._exhausted.values():
            exhausted.pop(TimeUpReason.ALLOWANCE, None)
        if self._activity is Activity.STOPPED:  # nobody has picked yet today
            self._watchers_all = True

    def _remaining_s(self, ids: Collection[int]) -> float | None:
        """PR-4: the minimum across the given profiles; None if all are unlimited."""
        values = [self._usages[pid].remaining_s(self._policies[pid].allowance_s) for pid in ids]
        finite = [v for v in values if v is not None]
        return min(finite) if finite else None

    def _max_session_s(self, pid: int) -> float | None:
        """WT-3: applies to ignore_pauses profiles that are not unlimited today."""
        p = self._policies[pid]
        if p.mode == CountingMode.IGNORE_PAUSES and not self._usages[pid].unlimited and p.max_session_s is not None:
            return p.max_session_s
        return None

    def _elapsed_s(self, pid: int, at: datetime) -> float | None:
        session = self._sessions.get(pid)
        return None if session is None else (at - session.start).total_seconds()

    def _decide(self, now: datetime) -> Decision:
        return self._decide_for(self.watchers, now)

    def _decide_for(self, ids: Collection[int], now: datetime) -> Decision:
        remaining = self._remaining_s(ids)
        blocked = any(self._usages[pid].blocked for pid in ids)
        exhausted = [(reason, e) for pid in ids for reason, e in self._exhausted.get(pid, {}).items()]
        reason, deadline, action = None, None, Action.CONTINUE
        if blocked:  # WT-7: stop now, no grace
            reason, action = TimeUpReason.BLOCKED, Action.STOP_NOW
        elif exhausted:  # the limit that stops playback soonest wins
            reason, e = min(exhausted, key=lambda item: item[1].stop_at)
            deadline = e.grace_deadline
            action = Action.FINISH_THEN_STOP if deadline and now < deadline else Action.STOP_NOW
        can_start = reason is None and (remaining is None or remaining > _EPS)
        starts = [self._sessions[pid].start for pid in ids if pid in self._sessions]
        started = min(starts, default=None)
        return Decision(
            action=action,
            reason=reason,
            grace_deadline=deadline,
            remaining_s=remaining,
            can_start=can_start,
            autoplay_allowed=can_start,  # WT-4: no autoplay once time is up
            session_started_at=started,
            session_elapsed_s=None if started is None else (now - started).total_seconds(),
        )

    def _day_of(self, when: datetime) -> date:
        return day_for(when, self._settings.reset_time, self._settings.tz)

    def _reset_after(self, when: datetime) -> datetime:
        return next_reset_after(when, self._settings.reset_time, self._settings.tz)


def _iso(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()


def _parse(value: str | None) -> datetime | None:
    return None if value is None else datetime.fromisoformat(value)
