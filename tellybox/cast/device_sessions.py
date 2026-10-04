"""Device sessions: episodes played in the kid app itself instead of on the TV (PB-7..PB-9, WT-10..WT-12).

The server cannot see the browser, so a device session is driven by heartbeats (about every 10 s,
WT-10) carrying the player's state and position. Each session is its own timer playback, keyed
``device:<device_id>`` (PB-8), beside the TV session (``CastController.current``, untouched by
this module). The controller mixes in ``DeviceSessionsMixin``; it needs no Chromecast.

Limits are enforced by answering heartbeats and by ending the watch session, which revokes the
session-scoped media URL (WT-11). An ended session leaves a short-lived tombstone so the device's
next heartbeat learns why it ended.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Collection
from dataclasses import dataclass
from datetime import datetime, timedelta

from tellybox import library, media_urls, store
from tellybox.cast.common import RESUME_TAIL_S, EndReason, PlayRefused, UnknownProfile
from tellybox.db import to_db
from tellybox.library import Episode
from tellybox.timer import TV, Action, Activity, TimeUpReason

log = logging.getLogger(__name__)

HEARTBEAT_LOST_S = 30.0      # WT-10: no heartbeat for this long and time stops counting
DEVICE_DISCONNECT_S = 300.0  # WT-10: no heartbeat for this long and the session ends
HEARTBEAT_CREDIT_CAP_S = 30.0  # WT-10: the most one heartbeat can credit as played time
PLAYED_FRACTION = 0.5        # PB-8: finished needs at least this share of the episode actually played
TOMBSTONE_S = 3600.0         # how long we remember why a device's session ended
DEVICE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
LABEL_MAX = 40

# What a device is told when its session ended for a reason it did not cause (the API's `reason`).
_END_REASONS = {
    EndReason.REPLACED: "replaced",
    EndReason.PARENT_STOP: "stop_now",
    EndReason.BLOCKED: "blocked",
    EndReason.TIME_UP: "time_up",
    EndReason.DISCONNECTED: "disconnected",
    EndReason.LOAD_FAILED: "error",
    EndReason.FINISHED: "finished",
}


def device_key(device_id: str) -> str:
    """The timer playback key of a device (PB-8)."""
    return f"device:{device_id}"


@dataclass
class DeviceSession:
    """An episode playing in a browser tab."""

    device_id: str
    label: str
    episode: Episode
    watch_session_id: int
    profile_ids: list[int]
    url: str
    started_at: datetime
    last_heartbeat_at: datetime
    position_at: datetime
    state: str = "loading"  # loading | playing | paused | buffering, as the last heartbeat said
    position_s: float = 0.0
    duration_s: float | None = None
    played_s: float = 0.0   # PB-8: time actually played, for the finished gate
    silent: bool = False    # counting stopped because the heartbeats did (WT-10)

    @property
    def key(self) -> str:
        return device_key(self.device_id)

    def position(self, now: datetime) -> float:
        pos = self.position_s
        if self.state == "playing":  # extrapolate between heartbeats, but not through a silence
            pos += min((now - self.position_at).total_seconds(), HEARTBEAT_LOST_S)
        if self.duration_s:
            pos = min(pos, self.duration_s)
        return max(0.0, pos)

    def finished(self, position_s: float) -> bool:
        """PB-8: at 95% and at least half of the episode really played; dragging to the end does not count."""
        return store.is_finished(position_s, self.duration_s) and self.played_s >= PLAYED_FRACTION * (self.duration_s or 0.0)


class DeviceSessionsMixin:
    """Device-session behaviour of ``CastController``; uses its conn, clock, timer and TV session."""

    device_sessions: dict[str, DeviceSession]  # by device_id
    _device_ended: dict[str, tuple[str | None, datetime]]  # device_id -> (reason for the device, when)

    # ------------------------------------------------------------------ commands

    async def device_play(self, device_id: str, label: str, episode_id: int, profile_ids: Collection[int]) -> dict:
        """The kids ``profile_ids`` picked an episode to watch on this device (PB-7). Replaces the
        device's own session and any other session of these profiles, TV included (PB-8).
        Returns ``{session, url, start_s}``."""
        if not DEVICE_ID_RE.fullmatch(device_id):
            raise ValueError("bad device id")
        episode = library.get_episode(self.conn, episode_id)
        if episode is None:
            raise KeyError(episode_id)
        if not profile_ids:
            raise ValueError("a pick needs at least one profile")
        now = self.clock.now()
        self._refresh_profiles(now)
        self._apply_position_shifts(now)  # SB-3
        profiles = sorted(set(profile_ids))
        if unknown := [p for p in profiles if p not in self._known_profiles]:
            raise UnknownProfile(unknown[0])
        decision = self.timer.group_decision(now, profiles)  # check first: a refusal must change nothing
        if not decision.can_start:
            self._broadcast()
            raise PlayRefused(decision)
        if old := self.device_sessions.get(device_id):
            self._end_device(old, EndReason.REPLACED, remember=False)
        self._device_ended.pop(device_id, None)
        self.timer.on_pick(now, profiles, device_key(device_id))
        await self._end_moved_sessions()
        answer = self._start_device_session(device_id, label, episode, profiles, now)
        self.persist(now)
        self._broadcast()
        return answer

    async def device_heartbeat(
        self, device_id: str, state: str, position_s: float, duration_s: float | None = None
    ) -> dict:
        """The browser's heartbeat (WT-10). The answer says whether to go on, stop or play the next episode."""
        now = self.clock.now()
        s = self.device_sessions.get(device_id)
        if s is None:
            reason, _ = self._device_ended.get(device_id, (None, now))
            return self._answer("stop", reason or "unknown_session", time_up=reason in ("time_up", "blocked"))
        self._refresh_profiles(now)
        if (now - s.last_heartbeat_at).total_seconds() >= DEVICE_DISCONNECT_S:
            self._end_device(s, EndReason.DISCONNECTED, ended_at=s.last_heartbeat_at)
            self._broadcast()
            return self._answer("stop", "disconnected")
        self._device_silence(s, now)

        # Played time (PB-8): the interval since the last heartbeat, if that one said playing, capped.
        if s.state in ("playing", "buffering"):
            s.played_s += min((now - s.last_heartbeat_at).total_seconds(), HEARTBEAT_CREDIT_CAP_S)
        if duration_s and duration_s > 0:
            s.duration_s = duration_s
        s.position_s = min(max(position_s, 0.0), s.duration_s) if s.duration_s else max(position_s, 0.0)
        s.position_at, s.last_heartbeat_at, s.silent = now, now, False

        if state == "error":  # PB-9: ends without counting anything for the failure
            self._end_device(s, EndReason.LOAD_FAILED)
            self._broadcast()
            return self._answer("stop", "error")
        if state == "ended":
            answer = await self._device_ended_episode(s, now)
            self._broadcast()
            return answer

        s.state = state
        decision = self.timer.set_activity(now, Activity.PAUSED if state == "paused" else Activity.PLAYING, s.key)
        if decision.action == Action.STOP_NOW:
            reason = EndReason.BLOCKED if decision.reason == TimeUpReason.BLOCKED else EndReason.TIME_UP
            self._end_device(s, reason)
            self._broadcast()
            return self._answer("stop", _END_REASONS[reason], time_up=True)
        answer = self._answer(
            "continue", None, time_up=not decision.can_start,
            grace_deadline=decision.grace_deadline if decision.action == Action.FINISH_THEN_STOP else None,
        )
        self._broadcast()
        return answer

    async def device_stop(self, device_id: str) -> None:
        """The kid left the player (STOPPED). Unknown devices are a no-op."""
        if s := self.device_sessions.get(device_id):
            self._end_device(s, EndReason.STOPPED, remember=False)
            self._broadcast()

    # ------------------------------------------------------------------ ending

    async def _device_ended_episode(self, s: DeviceSession, now: datetime) -> dict:
        """The player reached the end: autoplay the next episode unless time is up or autoplay is off (PB-3, WT-4)."""
        s.position_s, s.position_at = s.duration_s or s.position_s, now
        device_id, label, profiles, episode = s.device_id, s.label, list(s.profile_ids), s.episode
        self._end_device(s, EndReason.FINISHED, remember=False)
        show = library.get_show(self.conn, episode.show_id)
        nxt = library.next_episode(self.conn, episode.id) if show and show.autoplay else None
        if nxt is not None and profiles:
            decision = self.timer.group_decision(now, profiles)
            if decision.autoplay_allowed:
                decision = self.timer.on_pick(now, profiles, device_key(device_id))
                if decision.can_start:
                    await self._end_moved_sessions()
                    started = self._start_device_session(device_id, label, nxt, profiles, now)
                    return self._answer("next", None, next=started)
            reason = "blocked" if decision.reason == TimeUpReason.BLOCKED else "time_up"
            self._device_ended[device_id] = (reason, now)
            return self._answer("stop", reason, time_up=True)
        self._device_ended[device_id] = ("finished", now)
        return self._answer("stop", "finished")

    def _end_device(
        self, s: DeviceSession, reason: EndReason, ended_at: datetime | None = None, *, remember: bool = True
    ) -> None:
        """End a device session: save the position (gated finish, PB-8), close the watch session with the
        seconds this session counted, and drop its timer playback. ``remember`` leaves a tombstone so the
        device's next heartbeat can be told why."""
        now = self.clock.now()
        self._refresh_profiles(now)  # a profile deleted meanwhile must not get a position row
        self._device_silence(s, now)
        self.timer.set_activity(now, Activity.STOPPED, s.key)
        counted = self.timer.pop_counted_s(s.key)
        self.timer.end_playback(now, s.key)
        pos = s.position(now) if ended_at is None else s.position_s  # a lost connection keeps the last heartbeat's
        store.save_position(self.conn, s.profile_ids, s.episode.id, pos, s.finished(pos), now)
        store.close_watch_session(self.conn, s.watch_session_id, reason, ended_at or now, counted)
        log.info("device %s: episode %s ended: %s at %.0f s", s.device_id, s.episode.id, reason, pos)
        self.device_sessions.pop(s.device_id, None)
        if remember:
            self._device_ended[s.device_id] = (_END_REASONS.get(reason), now)
        self.persist(now)

    async def _end_moved_sessions(self) -> None:
        """PB-8: end the sessions that lost watchers to the last pick (a group's session ends together).
        The TV session stops the Chromecast like any stop; device sessions just end."""
        for key, lost in self.timer.moved_from().items():
            if key == TV:
                if self.current is not None and lost & set(self.current.profile_ids):
                    await self._stop_current(EndReason.REPLACED)
            elif (s := self.device_sessions.get(key.removeprefix("device:"))) is not None:
                self._end_device(s, EndReason.REPLACED)

    # ------------------------------------------------------------------ periodic

    def _tick_devices(self, now: datetime) -> None:
        """WT-10/WT-11: heartbeat gaps, then the per-session limits."""
        for s in list(self.device_sessions.values()):
            if (now - s.last_heartbeat_at).total_seconds() >= DEVICE_DISCONNECT_S:
                self._end_device(s, EndReason.DISCONNECTED, ended_at=s.last_heartbeat_at)
                continue
            self._device_silence(s, now)
            self._apply_device_decision(s, now)
        for device_id in [d for d, (_, at) in self._device_ended.items() if (now - at).total_seconds() > TOMBSTONE_S]:
            del self._device_ended[device_id]

    def _apply_device_decisions(self, now: datetime) -> None:
        for s in list(self.device_sessions.values()):
            self._apply_device_decision(s, now)

    def _apply_device_decision(self, s: DeviceSession, now: datetime) -> None:
        """Block and stop-now stop at once; time up once the grace is over (WT-4, WT-7)."""
        decision = self.timer.tick(now, s.key)
        if decision.action == Action.STOP_NOW:
            self._end_device(s, EndReason.BLOCKED if decision.reason == TimeUpReason.BLOCKED else EndReason.TIME_UP)

    def _device_silence(self, s: DeviceSession, now: datetime) -> None:
        """WT-10: after 30 s without a heartbeat nothing is known to play, so counting stops (at that moment)."""
        if not s.silent and (now - s.last_heartbeat_at).total_seconds() >= HEARTBEAT_LOST_S:
            self.timer.set_activity(s.last_heartbeat_at + timedelta(seconds=HEARTBEAT_LOST_S), Activity.STOPPED, s.key)
            s.silent = True

    def _persist_devices(self, now: datetime) -> None:
        """WT-8, PB-4: the history heartbeat and position of every device session (inside persist's transaction)."""
        for s in self.device_sessions.values():
            counted = self.timer.pop_counted_s(s.key)
            store.heartbeat_watch_session(self.conn, s.watch_session_id, counted, s.last_heartbeat_at)
            pos = s.position(now)
            store.save_position(self.conn, s.profile_ids, s.episode.id, pos, s.finished(pos), now)

    # ------------------------------------------------------------------ internals

    def _start_device_session(
        self, device_id: str, label: str, episode: Episode, profiles: list[int], now: datetime
    ) -> dict:
        start_s = 0.0
        saved = store.group_position(self.conn, profiles, episode.id)
        if saved is not None and (not episode.duration_s or saved < episode.duration_s - RESUME_TAIL_S):
            start_s = saved  # continue watching (PB-4)
        label = label.strip()[:LABEL_MAX]
        session_id = store.open_watch_session(self.conn, episode.id, profiles, now, target="device", device_label=label)
        expires_at = int(now.timestamp()) + media_urls.MEDIA_TTL_S
        s = DeviceSession(
            device_id=device_id,
            label=label,
            episode=episode,
            watch_session_id=session_id,
            profile_ids=list(profiles),
            url=media_urls.media_path(self.secret, episode.id, expires_at, session_id),
            started_at=now,
            last_heartbeat_at=now,
            position_at=now,
            position_s=start_s,
            duration_s=episode.duration_s,
        )
        self.device_sessions[device_id] = s
        log.info("device %s: playing episode %s from %.0f s", device_id, episode.id, start_s)
        return {"session": self._device_session_state(s, now), "url": s.url, "start_s": start_s}

    @staticmethod
    def _answer(
        action: str, reason: str | None, *, time_up: bool = False, grace_deadline: datetime | None = None,
        next: dict | None = None,
    ) -> dict:
        answer = {"action": action, "reason": reason, "time_up": time_up, "grace_deadline": to_db(grace_deadline)}
        if next is not None:
            answer["next"] = next
        return answer

    @staticmethod
    def _device_session_state(s: DeviceSession, now: datetime) -> dict:
        return {
            "key": s.key,
            "target": "device",
            "label": s.label,
            "device_id": s.device_id,
            "episode_id": s.episode.id,
            "show_id": s.episode.show_id,
            "title": s.episode.title,
            "state": s.state,
            "position_s": round(s.position(now)),
            "duration_s": s.duration_s,
            "profile_ids": sorted(s.profile_ids),
        }
