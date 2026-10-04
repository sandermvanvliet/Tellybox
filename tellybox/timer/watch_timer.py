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

Playbacks (PB-8): the timer times several concurrent *playbacks*, each with its
own activity and watchers, named by a key. ``TV`` always exists; device playbacks
(browser tabs) are created by ``set_activity``/``set_watchers``/``on_pick`` with
their key and dropped by ``end_playback``. A profile watches at most one playback:
a pick that puts it in another playback takes it out of the first (see
``moved_from``). Every method that implies a playback takes a trailing ``key``
that defaults to ``TV``, so single-playback callers are unchanged.
"""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass, field, replace
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
_SNAPSHOT_VERSION = 2  # PB-8 adds a "playbacks" map without a bump: older readers ignore it
TV = "tv"  # PB-8: the key of the TV playback, which always exists


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


@dataclass
class _Playback:
    """One concurrent playback (PB-8): its activity and the profiles watching it."""

    activity: Activity = Activity.STOPPED
    watchers: set[int] = field(default_factory=set)


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
        self._playbacks: dict[str, _Playback] = {TV: _Playback()}
        self._watchers_all = True  # TV only: no pick yet (today), every profile counts, as in v1
        self._moved: dict[str, frozenset[int]] = {}
        self._sessions: dict[int, _Session] = {}
        self._exhausted: dict[int, dict[TimeUpReason, _Exhaustion]] = {}  # ALLOWANCE / SESSION_MAX only
        self._dirty: set[int] = set()
        self._dirty_prev: list[DayUsage] = []  # final rows of days rolled over
        self._counted_s = 0.0
        self._counted_by: dict[str, float] = {}
        if snapshot is not None:
            self._restore(snapshot)
        self._check(now)

    # -- public API -------------------------------------------------------

    @property
    def activity(self) -> Activity:
        """The TV playback's activity."""
        return self._playbacks[TV].activity

    def activity_of(self, key: str) -> Activity:
        pb = self._playbacks.get(key)
        return Activity.STOPPED if pb is None else pb.activity

    def tick(self, now: datetime, key: str = TV) -> Decision:
        return self._decide(self._sync(now), key)

    def decision(self, now: datetime, key: str = TV) -> Decision:
        return self.tick(now, key)

    def set_activity(self, now: datetime, activity: Activity, key: str = TV) -> Decision:
        now = self._sync(now)  # accrue under the old activity first
        pb = self._playback(key)
        old, pb.activity = pb.activity, activity
        watchers = self.watchers_of(key)  # PB-8: only this playback's watchers
        if activity is Activity.PLAYING:
            for pid in watchers:
                session = self._sessions.get(pid)
                if session is None:  # WT-3: first PLAYING starts a session
                    self._sessions[pid] = _Session(now)
                else:
                    session.inactive_since = None
        elif old is Activity.PLAYING:
            for pid in watchers:
                if pid in self._sessions:
                    self._sessions[pid].inactive_since = now  # WT-3: the session break starts counting
        self._check(now)
        return self._decide(now, key)

    @property
    def watchers(self) -> frozenset[int]:
        """The profiles watching the current TV pick (PR-2). Only they accrue time (PR-4)."""
        return self.watchers_of(TV)

    def watchers_of(self, key: str) -> frozenset[int]:
        if key == TV and self._watchers_all:  # everyone, except profiles that watch a device (PB-8)
            return frozenset(self._policies) - frozenset(
                pid for k, pb in self._playbacks.items() if k != TV for pid in pb.watchers
            )
        pb = self._playbacks.get(key)
        return frozenset() if pb is None else frozenset(pb.watchers)

    def keys(self) -> frozenset[str]:
        """The keys of all playbacks, TV included."""
        return frozenset(self._playbacks)

    def moved_from(self) -> dict[str, frozenset[int]]:
        """PB-8: the playbacks (key -> profiles) that lost watchers to the last successful
        ``on_pick``/``set_watchers``, so the caller can end those sessions. Empty otherwise."""
        return dict(self._moved)

    def set_watchers(self, now: datetime, profile_ids: Collection[int], key: str = TV) -> Decision:
        """Make ``profile_ids`` the watchers without checking whether they may start;
        used when the cast service re-attaches to playback after a restart."""
        ids = self._resolve(profile_ids)
        now = self._sync(now)
        self._attach(now, ids, False, key=key)
        self._check(now)
        return self._decide(now, key)

    def end_playback(self, now: datetime, key: str) -> None:
        """PB-8: drop a device playback; its watchers start their session break (WT-3)."""
        if key == TV:
            raise ValueError("the TV playback cannot be ended")
        now = self._sync(now)
        pb = self._playbacks.pop(key, None)
        for pid in () if pb is None else pb.watchers:
            session = self._sessions.get(pid)
            if session is not None and session.inactive_since is None:
                session.inactive_since = now
        self._check(now)

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
            watching=any(profile_id in self.watchers_of(k) for k in self._playbacks),
        )

    def on_pick(self, now: datetime, profile_ids: Collection[int] | None = None, key: str = TV) -> Decision:
        """A pick by ``profile_ids`` (None: every profile, the v1 behaviour) on playback ``key``.
        If the group may start, it becomes that playback's watchers and each member's viewing
        session starts or extends. Members watching elsewhere leave there (PB-8, ``moved_from``)."""
        ids = self._resolve(profile_ids)
        now = self._sync(now)
        decision = self._decide_for(ids, now)
        self._moved = {}
        if not decision.can_start:
            return decision
        self._attach(now, ids, True, everyone=profile_ids is None, key=key)
        return self._decide(now, key)

    def _resolve(self, profile_ids: Collection[int] | None) -> frozenset[int]:
        ids = frozenset(self._policies) if profile_ids is None else frozenset(profile_ids)
        if not ids:
            raise ValueError("a group needs at least one profile")
        unknown = ids - self._policies.keys()
        if unknown:
            raise KeyError(sorted(unknown)[0])
        return ids

    def _playback(self, key: str) -> _Playback:
        return self._playbacks.setdefault(key, _Playback())

    def _attach(self, now: datetime, ids: frozenset[int], pick: bool, everyone: bool = False, key: str = TV) -> None:
        """Make ``ids`` the watchers of ``key``. Members that stop watching start their session
        break now; a pick starts or extends the members' sessions (WT-3). A profile watches one
        playback only (PB-8): members of other playbacks are taken out of them."""
        pb = self._playback(key)
        playing = pb.activity is Activity.PLAYING
        self._moved = {}
        for pid in self.watchers_of(key) - ids:
            session = self._sessions.get(pid)
            if session is not None and session.inactive_since is None:
                session.inactive_since = now
        for other in list(self._playbacks):
            lost = self.watchers_of(other) & ids if other != key else frozenset()
            if lost:
                self._remove_watchers(other, lost)
                self._moved[other] = lost
        for pid in ids:
            session = self._sessions.get(pid)
            if session is None:
                if pick or playing:
                    self._sessions[pid] = _Session(now, None if playing else now)
            elif playing:
                session.inactive_since = None
            elif pick:
                session.inactive_since = now
        if key == TV:
            self._watchers_all = everyone
        pb.watchers = set(ids)

    def _remove_watchers(self, key: str, ids: frozenset[int]) -> None:
        """Take ``ids`` out of a playback; their sessions continue in the playback they move to."""
        pb = self._playbacks[key]
        if key == TV and self._watchers_all:  # make the implicit "everyone" explicit
            pb.watchers = set(self._policies)
            self._watchers_all = False
        pb.watchers -= ids

    def _playback_of(self, pid: int) -> str | None:
        """The key of the playback ``pid`` watches (an explicit one wins over TV's implicit everyone)."""
        for key, pb in self._playbacks.items():
            if pid in pb.watchers and not (key == TV and self._watchers_all):
                return key
        return TV if self._watchers_all else None

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
        for pb in self._playbacks.values():
            pb.watchers &= self._policies.keys()
        if not self._playbacks[TV].watchers:
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

    def pop_counted_s(self, key: str | None = None) -> float:
        """Seconds counted since the last pop. ``None``: the total over all playbacks, which
        resets every share; with a key, that playback's share (also taken off the total)."""
        if key is None:
            counted, self._counted_s = self._counted_s, 0.0
            self._counted_by.clear()
            return counted
        counted = self._counted_by.pop(key, 0.0)
        self._counted_s = max(0.0, self._counted_s - counted)
        return counted

    def snapshot(self) -> dict:
        """State as of the last call, for WT-8. Usage is persisted separately."""
        sessions = {}
        for pid, session in self._sessions.items():
            key = self._playback_of(pid)
            watching_now = key is not None and self.activity_of(key) is Activity.PLAYING
            last_active = self._t if watching_now else session.inactive_since
            sessions[str(pid)] = {"started_at": _iso(session.start), "last_active_at": _iso(last_active)}
        return {
            "version": _SNAPSHOT_VERSION,
            "as_of": self._t.isoformat(),
            "day": self._day.isoformat(),
            "activity": self.activity.value,
            "watchers": None if self._watchers_all else sorted(self._playbacks[TV].watchers),
            "playbacks": {  # PB-8: informational beyond TV; only TV is restored
                key: {"watchers": None if key == TV and self._watchers_all else sorted(pb.watchers)}
                for key, pb in self._playbacks.items()
            },
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
            watchers = snap.get("playbacks", {}).get(TV, {"watchers": snap["watchers"]})["watchers"]
        if watchers is not None:
            self._playbacks[TV].watchers = {int(pid) for pid in watchers} & self._policies.keys()
            self._watchers_all = not self._playbacks[TV].watchers
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
        for key, pb in self._playbacks.items():
            for pid in self.watchers_of(key):
                p = self._policies[pid]
                if TimeUpReason.ALLOWANCE not in self._exhausted.get(pid, {}):
                    remaining = self._usages[pid].remaining_s(p.allowance_s)
                    if remaining is not None and self._counts(p, pb.activity):
                        yield t + timedelta(seconds=remaining)
        for pid, session in self._sessions.items():
            max_s = self._max_session_s(pid)
            if max_s is not None and TimeUpReason.SESSION_MAX not in self._exhausted.get(pid, {}):
                yield session.start + timedelta(seconds=max_s)
            if session.inactive_since is not None:
                yield session.inactive_since + timedelta(seconds=self._settings.session_break_s)

    def _counts(self, p: ProfilePolicy, activity: Activity) -> bool:
        """WT-2: ignore_pauses counts playing only; wall_clock also counts paused."""
        return activity is Activity.PLAYING or (activity is Activity.PAUSED and p.mode == CountingMode.WALL_CLOCK)

    def _accrue(self, seconds: float) -> None:
        for key, pb in self._playbacks.items():  # PB-8: each playback's watchers pay for it
            counted = False
            for pid in self.watchers_of(key):  # PR-4 / A-1: every watching profile pays, and only they
                if self._counts(self._policies[pid], pb.activity):
                    self._usages[pid].used_s += seconds
                    self._dirty.add(pid)
                    counted = True
            if counted:
                self._counted_s += seconds
                self._counted_by[key] = self._counted_by.get(key, 0.0) + seconds

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
        # PB-8: judged per profile, by the playback it watches; in none counts as stopped.
        grace = _Exhaustion(at, at + timedelta(seconds=self._settings.grace_cap_s))
        stopped = _Exhaustion(at, None)

        for pid, p in self._policies.items():
            key = self._playback_of(pid)
            hit = grace if key is not None and self.activity_of(key) is not Activity.STOPPED else stopped
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
        if self.activity is Activity.STOPPED:  # nobody has picked yet today
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

    def _decide(self, now: datetime, key: str = TV) -> Decision:
        return self._decide_for(self.watchers_of(key), now)

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
