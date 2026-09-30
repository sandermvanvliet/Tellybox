"""Cast controller: the single owner of the Chromecast connection and the watch timer.

The web app sends commands (play, pause, override, ...) through the cast API;
the controller pushes state snapshots to subscribers (SSE, KA-7).

Spike findings applied here (docs/spike-casting.md):
- no periodic status while playing -> the timer runs on our clock, positions are extrapolated;
- other apps' media status reaches us -> only our receiver session + our URL count (WT-9);
- FINISHED reports position 0 -> positions are tracked here (PB-4);
- the device buffers ahead -> stopping is always an explicit stop command.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sqlite3
from collections.abc import Collection
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from zoneinfo import ZoneInfo

from tellybox import library, media_urls, store
from tellybox.cast.device import (
    DEFAULT_MEDIA_RECEIVER,
    CastDevice,
    ConnectionState,
    ConnectionStatus,
    DeviceEvent,
    LoadFailed,
    MediaStatus,
    PlayerState,
    ReceiverStatus,
)
from tellybox.cast.pychromecast_device import CastCommandError
from tellybox.clock import Clock
from tellybox.db import to_db
from tellybox.library import Episode
from tellybox.timer import Action, Activity, Decision, TimeUpReason, WatchTimer, day_for

log = logging.getLogger(__name__)

TICK_S = 1.0
PERSIST_EVERY_S = 15.0       # WT-8: at least every 30 s
STATUS_POLL_EVERY_S = 30.0   # drift check; the device sends nothing while playing
RECOVERY_WAIT_S = 10.0       # NF-7: how long to wait for our media to show up after a restart
RECONNECT_WAIT_S = 300.0     # PB-5: how long a lost connection may last before the session is ended
RESUME_TAIL_S = 10.0         # don't resume within the last seconds of an episode


class EndReason(StrEnum):
    FINISHED = "finished"
    REPLACED = "replaced"          # a new pick replaced it (A-5)
    STOPPED = "stopped"            # stop from the kid app or another controller
    PARENT_STOP = "parent_stop"    # WT-7 stop now
    TIME_UP = "time_up"            # WT-4/WT-5/WT-3
    BLOCKED = "blocked"            # WT-7 block
    TAKEN_OVER = "taken_over"      # another app or cast took the device (WT-9, PB-5)
    DISCONNECTED = "disconnected"  # PB-5
    RESTART = "restart"            # our process restarted and the media was gone (NF-7)
    LOAD_FAILED = "load_failed"


class PlayRefused(Exception):
    def __init__(self, decision: Decision) -> None:
        super().__init__(f"play refused: {decision.reason}")
        self.decision = decision


class NoDevice(Exception):
    pass


class UnknownProfile(ValueError):
    """A pick named a profile that does not exist (PR-2)."""


@dataclass
class Current:
    """The episode Tellybox is playing right now."""

    episode: Episode
    url: str
    watch_session_id: int
    profile_ids: list[int]
    cast_session_id: str | None = None
    player_state: PlayerState = PlayerState.UNKNOWN
    position_s: float = 0.0
    position_at: datetime | None = None
    duration_s: float | None = None
    stopping: bool = False
    # Receiver media sessions: ours once seen, and the one our load replaced. The replaced one's
    # IDLE status can arrive carrying our URL (pychromecast keeps the last contentId it saw).
    media_session_id: int | None = None
    replaced_media_session_id: int | None = None

    def position(self, now: datetime) -> float:
        pos = self.position_s
        if self.player_state == PlayerState.PLAYING and self.position_at is not None:
            pos += (now - self.position_at).total_seconds()
        if self.duration_s:
            pos = min(pos, self.duration_s)
        return max(0.0, pos)


class CastController:
    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        clock: Clock,
        tz: ZoneInfo,
        media_base_url: str,
        secret: bytes,
        device: CastDevice | None = None,
    ) -> None:
        self.conn = conn
        self.clock = clock
        self.tz = tz
        self.media_base_url = media_base_url
        self.secret = secret
        self.device = device
        self.connection = ConnectionState.DISCONNECTED
        self.current: Current | None = None

        now = clock.now()
        settings = store.timer_settings(conn, tz)
        self.timer = WatchTimer(
            settings,
            store.profile_policies(conn),
            store.load_usages(conn, day_for(now, settings.reset_time, tz)),
            now,
            store.load_timer_snapshot(conn),
        )
        self._known_profiles = store.profile_ids(conn)
        self._decision = self.timer.tick(now)
        self._lost_at: datetime | None = None
        self._reconnected = False  # first receiver status after a reconnect decides between resume and PB-5
        self._recovering: store.OpenSession | None = None
        self._recover_deadline: datetime | None = None
        self._last_persist = now
        self._last_poll = now
        self._subscribers: set[asyncio.Queue] = set()
        self._last_broadcast: dict | None = None
        self._tasks: list[asyncio.Task] = []
        self._device_task: asyncio.Task | None = None

    # ------------------------------------------------------------------ lifecycle

    async def start(self, run_loops: bool = True) -> None:
        """Restore state after a (re)start (NF-7) and connect to the device."""
        now = self.clock.now()
        open_sessions = store.open_watch_sessions(self.conn)
        for stale in open_sessions[:-1]:
            store.close_watch_session(self.conn, stale.id, EndReason.RESTART, stale.last_heartbeat_at or stale.started_at)
        if open_sessions:
            self._recovering = open_sessions[-1]
            self._recover_deadline = now + timedelta(seconds=RECOVERY_WAIT_S)
            log.info("recovering watch session %s (episode %s)", self._recovering.id, self._recovering.episode_id)
        if run_loops:
            self._tasks.append(asyncio.create_task(self._tick_loop(), name="cast-tick"))
            if self.device is not None:
                self._device_task = asyncio.create_task(self._device_loop(self.device), name="cast-device")
        elif self.device is not None:
            await self.device.connect()

    async def stop_service(self) -> None:
        self.persist(self.clock.now())
        for task in [*self._tasks, self._device_task]:
            if task:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
        if self.device is not None:
            with contextlib.suppress(Exception):
                await self.device.close()

    async def set_device(self, device: CastDevice) -> None:
        """Switch to another Chromecast (PB-1). Whatever plays on the old one is ended."""
        if self.current:
            await self._stop_current(EndReason.STOPPED)
        if self._device_task:
            self._device_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._device_task
        if self.device is not None:
            with contextlib.suppress(Exception):
                await self.device.close()
        self.device = device
        self.connection = ConnectionState.DISCONNECTED
        self._device_task = asyncio.create_task(self._device_loop(device), name="cast-device")
        self._broadcast()

    async def _device_loop(self, device: CastDevice) -> None:
        while True:
            try:
                await device.connect()
                async for event in device.events():
                    await self.handle_event(event)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("device connection failed; retrying in 10 s")
                self.connection = ConnectionState.FAILED
                self._broadcast()
                await asyncio.sleep(10)

    async def _tick_loop(self) -> None:
        while True:
            try:
                await self.tick()
            except Exception:
                log.exception("tick failed")
            await asyncio.sleep(TICK_S)

    # ------------------------------------------------------------------ commands

    async def play(self, episode_id: int, profile_ids: Collection[int] | None = None) -> None:
        """The kids ``profile_ids`` picked an episode (KA-5, PR-2). Replaces whatever plays (A-5).
        None means every profile; an empty list is an error. The group starts only if every
        member may (PR-4)."""
        if self.device is None:
            raise NoDevice()
        episode = library.get_episode(self.conn, episode_id)
        if episode is None:
            raise KeyError(episode_id)
        now = self.clock.now()
        self._refresh_profiles(now)
        self._apply_position_shifts(now)  # SB-3: resume from the remapped position
        if profile_ids is None:
            profiles = list(self._known_profiles)
        elif not profile_ids:
            raise ValueError("a pick needs at least one profile")
        else:
            profiles = sorted(set(profile_ids))
            if unknown := [p for p in profiles if p not in self._known_profiles]:
                raise UnknownProfile(unknown[0])
        decision = self.timer.on_pick(now, profiles)
        self._decision = decision
        if not decision.can_start:
            self._broadcast()
            raise PlayRefused(decision)
        await self._start_episode(episode, now, profiles)

    async def pause(self) -> None:
        if self.current and self.device:
            await self.device.pause()

    async def resume(self) -> None:
        if self.current and self.device:
            await self.device.resume()

    async def stop(self) -> None:
        await self._stop_current(EndReason.STOPPED)

    async def override(
        self,
        kind: str,
        value: int | None = None,
        profile_ids: Collection[int] | None = None,
        source: str | None = None,
    ) -> None:
        """Parent overrides for today (WT-7, HA-3): extra_minutes, unlimited, block, stop_now, clear.
        ``profile_ids`` None means every profile; stop_now ignores it. ``source`` names the API token (HA-7)."""
        now = self.clock.now()
        self._refresh_profiles(now)
        if profile_ids is None:
            profiles = list(self._known_profiles)
        else:
            profiles = sorted(set(profile_ids))
            unknown = [p for p in profiles if p not in self._known_profiles]
            if unknown:
                raise UnknownProfile(unknown[0])
        if kind == "extra_minutes" and (not value or value <= 0):
            raise ValueError("extra_minutes needs a positive value")
        if kind not in ("extra_minutes", "unlimited", "block", "stop_now", "clear"):
            raise ValueError(f"unknown override {kind!r}")
        if kind == "stop_now":
            await self._stop_current(EndReason.PARENT_STOP)
        for p in profiles:
            if kind == "extra_minutes":
                self.timer.add_extra(now, p, value * 60.0)
            elif kind == "unlimited":
                self.timer.set_unlimited(now, p, value is None or bool(value))
            elif kind == "block":
                self.timer.set_blocked(now, p, value is None or bool(value))
            elif kind == "clear":
                self.timer.set_unlimited(now, p, False)
                self.timer.set_blocked(now, p, False)
            store.log_override(self.conn, p, self.timer.usage(p).day, kind, value, now, source)
        self._decision = self.timer.tick(now)
        await self._apply_decision(self._decision)
        self.persist(now)
        self._broadcast()

    # ------------------------------------------------------------------ periodic

    async def tick(self) -> None:
        now = self.clock.now()
        if self._recovering and self._recover_deadline and now >= self._recover_deadline:
            self._give_up_recovery()
        if self.current and self._lost_at and (now - self._lost_at).total_seconds() >= RECONNECT_WAIT_S:
            self._end_current(EndReason.DISCONNECTED, ended_at=self._lost_at)
        self._apply_position_shifts(now)
        self._refresh_profiles(now)
        self._decision = self.timer.tick(now)
        await self._apply_decision(self._decision)
        if (
            self.current
            and self.device
            and self.connection == ConnectionState.CONNECTED
            and (now - self._last_poll).total_seconds() >= STATUS_POLL_EVERY_S
        ):
            self._last_poll = now
            with contextlib.suppress(CastCommandError):
                await self.device.request_status()
        if (now - self._last_persist).total_seconds() >= PERSIST_EVERY_S:
            self.persist(now)
        self._broadcast()

    def _apply_position_shifts(self, now: datetime) -> None:
        """SB-3: positions the worker queued after replacing a file."""
        if applied := store.apply_position_shifts(self.conn, now):
            log.info("moved saved positions for %d replaced file(s) (SB-3)", applied)

    def persist(self, now: datetime) -> None:
        """Write timer state, usage, history heartbeat and position (WT-8, PB-4)."""
        self.conn.execute("BEGIN")
        try:
            # Pick up admin changes to allowances and settings (AD-2), and to the profiles (PR-1).
            self._refresh_profiles(now, force=True)
            counted = self.timer.pop_counted_s()
            store.save_usages(self.conn, self.timer.pop_dirty_usage(), now)
            store.save_timer_snapshot(self.conn, self.timer.snapshot(), now)
            if self.current:
                c = self.current
                store.heartbeat_watch_session(self.conn, c.watch_session_id, counted, now)
                pos = c.position(now)
                store.save_position(self.conn, c.profile_ids, c.episode.id, pos, store.is_finished(pos, c.duration_s), now)
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        self._last_persist = now

    # ------------------------------------------------------------------ device events

    async def handle_event(self, event: DeviceEvent) -> None:
        now = self.clock.now()
        match event:
            case ConnectionStatus(state=state):
                self._on_connection(state, now)
            case ReceiverStatus():
                self._on_receiver(event, now)
            case MediaStatus():
                await self._on_media(event, now)
            case LoadFailed():
                if self.current and not self.current.stopping:
                    log.warning("load failed: %s", event.error_code)
                    self._end_current(EndReason.LOAD_FAILED)
        self._broadcast()

    def _on_connection(self, state: ConnectionState, now: datetime) -> None:
        self.connection = state
        if state in (ConnectionState.LOST, ConnectionState.FAILED, ConnectionState.DISCONNECTED):
            if self._lost_at is None:
                self._lost_at = now
                if self.current:
                    # Freeze counting: we can't see what happens on the TV (conservative, like a restart).
                    self._decision = self.timer.set_activity(now, Activity.STOPPED)
        elif state == ConnectionState.CONNECTED:
            self._reconnected = self._lost_at is not None
            self._lost_at = None

    def _on_receiver(self, event: ReceiverStatus, now: datetime) -> None:
        reconnected, self._reconnected = self._reconnected, False
        c = self.current
        if c is None or c.stopping:
            return
        if c.cast_session_id is None:
            if event.app_id == DEFAULT_MEDIA_RECEIVER and event.session_id:
                self._adopt_cast_session(c, event.session_id)
            return
        if event.session_id != c.cast_session_id:
            # Our receiver session is gone: someone cast something else, or the device rebooted (PB-5, WT-9).
            self._end_current(EndReason.DISCONNECTED if reconnected else EndReason.TAKEN_OVER)

    async def _on_media(self, event: MediaStatus, now: datetime) -> None:
        if self._recovering:
            self._try_recover(event, now)
        c = self.current
        if c is None or c.stopping or event.content_id is None:
            return
        if event.content_id != c.url:
            if media_urls.episode_id_from_url(event.content_id) is not None:
                return  # late status for an earlier Tellybox load
            self._end_current(EndReason.TAKEN_OVER)  # other media loaded into the receiver (WT-9)
            return

        sid = event.media_session_id
        if sid is not None:
            if sid == c.replaced_media_session_id:
                return  # the media our load replaced, reported under our URL
            if event.player_state != PlayerState.IDLE:
                c.media_session_id = sid  # only live media plays, buffers or pauses
            elif c.media_session_id is not None and sid != c.media_session_id:
                return  # an earlier session ending

        if c.cast_session_id is None and self.device and self.device.receiver:
            r = self.device.receiver
            if r.app_id == DEFAULT_MEDIA_RECEIVER and r.session_id:
                self._adopt_cast_session(c, r.session_id)

        state = event.player_state
        if state in (PlayerState.PLAYING, PlayerState.BUFFERING, PlayerState.PAUSED):
            c.position_s = event.current_time
            c.position_at = now
            if event.duration:
                c.duration_s = event.duration
            # BUFFERING counts as playing (owner decision, 2026-09-27).
            activity = Activity.PAUSED if state == PlayerState.PAUSED else Activity.PLAYING
            self._decision = self.timer.set_activity(now, activity)
            c.player_state = state  # BUFFERING doesn't advance the extrapolated position
        elif state == PlayerState.IDLE:
            if event.idle_reason == "FINISHED":
                await self._on_finished(now)
            elif event.idle_reason in ("CANCELLED", "INTERRUPTED"):
                self._end_current(EndReason.STOPPED)  # stopped from another controller, e.g. a phone
            elif event.idle_reason == "ERROR":
                self._end_current(EndReason.LOAD_FAILED)
            # IDLE without a reason is the load step; ignore.

    async def _on_finished(self, now: datetime) -> None:
        """Episode ended: autoplay the next one unless time is up or autoplay is off (PB-3, WT-4)."""
        c = self.current
        assert c is not None
        c.position_s = c.duration_s or c.position(now)
        c.position_at = now
        c.player_state = PlayerState.IDLE
        self._end_current(EndReason.FINISHED)

        self._decision = self.timer.tick(now)
        show = library.get_show(self.conn, c.episode.show_id)
        nxt = None
        if show and show.autoplay and self._decision.autoplay_allowed:
            nxt = library.next_episode(self.conn, c.episode.id)
        if nxt is not None and c.profile_ids:
            decision = self.timer.on_pick(now, c.profile_ids)  # autoplay keeps the group
            self._decision = decision
            if decision.can_start:
                await self._start_episode(nxt, now, c.profile_ids)
                return
        if self.device:
            with contextlib.suppress(CastCommandError):
                await self.device.stop()  # back to the TV's idle screen

    # ------------------------------------------------------------------ internals

    async def _start_episode(self, episode: Episode, now: datetime, profiles: list[int]) -> None:
        if self.device is None:
            raise NoDevice()
        if self.current:
            self._end_current(EndReason.REPLACED)
        start_s = 0.0
        saved = store.group_position(self.conn, profiles, episode.id)
        if saved is not None:
            if not episode.duration_s or saved < episode.duration_s - RESUME_TAIL_S:
                start_s = saved  # continue watching (PB-4)
        url = media_urls.media_url(self.media_base_url, self.secret, episode.id, now)
        session_id = store.open_watch_session(self.conn, episode.id, profiles, now)
        self.current = Current(
            episode=episode,
            url=url,
            watch_session_id=session_id,
            profile_ids=list(profiles),
            position_s=start_s,
            position_at=now,
            duration_s=episode.duration_s,
        )
        receiver = self.device.receiver
        if receiver and receiver.app_id == DEFAULT_MEDIA_RECEIVER and receiver.session_id:
            self._adopt_cast_session(self.current, receiver.session_id)  # warm receiver: no new session event
            if self.device.media is not None:
                self.current.replaced_media_session_id = self.device.media.media_session_id
        log.info("playing episode %s from %.0f s", episode.id, start_s)
        try:
            await self.device.play(url, title=episode.title, start_s=start_s)
        except CastCommandError:
            self._end_current(EndReason.LOAD_FAILED)
            raise
        self._broadcast()

    def _adopt_cast_session(self, c: Current, cast_session_id: str) -> None:
        c.cast_session_id = cast_session_id
        store.set_cast_session_id(self.conn, c.watch_session_id, cast_session_id)

    async def _stop_current(self, reason: EndReason) -> None:
        c = self.current
        if c is None:
            return
        c.stopping = True
        if self.device:
            try:
                await self.device.stop()
            except CastCommandError:
                log.warning("stop command failed; ending the session anyway")
        self._end_current(reason)

    def _end_current(self, reason: EndReason, ended_at: datetime | None = None) -> None:
        c = self.current
        if c is None:
            return
        now = self.clock.now()
        self._refresh_profiles(now)  # a profile deleted meanwhile must not get a position row
        self._decision = self.timer.set_activity(now, Activity.STOPPED)
        counted = self.timer.pop_counted_s()
        pos = c.position(ended_at or now)
        finished = reason == EndReason.FINISHED or store.is_finished(pos, c.duration_s)
        store.save_position(self.conn, c.profile_ids, c.episode.id, pos, finished, now)
        store.close_watch_session(self.conn, c.watch_session_id, reason, ended_at or now, counted)
        log.info("episode %s ended: %s at %.0f s", c.episode.id, reason, pos)
        self.current = None
        self.persist(now)

    async def _apply_decision(self, decision: Decision) -> None:
        if self.current and decision.action == Action.STOP_NOW:
            reason = EndReason.BLOCKED if decision.reason == TimeUpReason.BLOCKED else EndReason.TIME_UP
            await self._stop_current(reason)

    def _try_recover(self, event: MediaStatus, now: datetime) -> None:
        rs = self._recovering
        assert rs is not None
        if (
            event.content_id
            and rs.episode_id is not None
            and media_urls.episode_id_from_url(event.content_id) == rs.episode_id
            and event.player_state in (PlayerState.PLAYING, PlayerState.BUFFERING, PlayerState.PAUSED)
        ):
            episode = library.get_episode(self.conn, rs.episode_id)
            if episode is None:
                self._give_up_recovery()
                return
            receiver = self.device.receiver if self.device else None
            self._refresh_profiles(now)
            watchers = [p for p in rs.profile_ids if p in self._known_profiles] or list(self._known_profiles)
            self.current = Current(
                episode=episode,
                url=event.content_id,
                watch_session_id=rs.id,
                profile_ids=watchers,
                cast_session_id=receiver.session_id if receiver and receiver.app_id == DEFAULT_MEDIA_RECEIVER else rs.cast_session_id,
                duration_s=episode.duration_s,
            )
            self._recovering = None
            self._decision = self.timer.set_watchers(now, watchers)  # the same kids keep paying (PR-4)
            log.info("re-attached to episode %s after restart", episode.id)

    def _give_up_recovery(self) -> None:
        rs = self._recovering
        if rs:
            store.close_watch_session(self.conn, rs.id, EndReason.RESTART, rs.last_heartbeat_at or rs.started_at)
            log.info("closed watch session %s left open by a restart", rs.id)
        self._recovering = None
        self._recover_deadline = None

    def _refresh_profiles(self, now: datetime, force: bool = False) -> None:
        """Follow profiles added or deleted in the admin: the timer learns the new set, and a
        deleted profile leaves the current episode (its rows are gone, so no more positions)."""
        ids = store.profile_ids(self.conn)
        if ids != self._known_profiles or force:
            self.timer.set_policies(store.timer_settings(self.conn, self.tz), store.profile_policies(self.conn), now)
            self._known_profiles = ids
        if self.current and any(p not in ids for p in self.current.profile_ids):
            self.current.profile_ids = [p for p in self.current.profile_ids if p in ids]

    # ------------------------------------------------------------------ state (KA-6, KA-7, AD-3)

    def state(self) -> dict:
        now = self.clock.now()
        self._refresh_profiles(now)
        d = self._decision
        c = self.current
        now_playing = None
        if c is not None:
            state = c.player_state
            now_playing = {
                "episode_id": c.episode.id,
                "show_id": c.episode.show_id,
                "title": c.episode.title,
                "state": "loading" if state in (PlayerState.UNKNOWN, PlayerState.IDLE) else state.value.lower(),
                "position_s": round(c.position(now)),
                "duration_s": c.duration_s,
                "profile_ids": sorted(c.profile_ids),
            }
        info = self.device.info if self.device else None
        return {
            "connection": self.connection.value,
            "device": {"uuid": info.uuid, "name": info.name} if info else None,
            "now_playing": now_playing,
            "timer": {
                "remaining_s": None if d.remaining_s is None else round(d.remaining_s),
                "can_start": d.can_start,
                "action": d.action.value,
                "reason": d.reason.value if d.reason else None,
                "grace_deadline": to_db(d.grace_deadline),
                "session_started_at": to_db(d.session_started_at),
                "session_elapsed_s": None if d.session_elapsed_s is None else round(d.session_elapsed_s),
                "next_reset": to_db(self.timer.next_reset),  # HA-2
                "profiles": [self._profile_state(now, p) for p in self._known_profiles],
            },
            "time_up": not d.can_start,  # KA-9
        }

    def _profile_state(self, now: datetime, profile_id: int) -> dict:
        u, status = self.timer.usage(profile_id), self.timer.profile_status(now, profile_id)
        return {
            "profile_id": u.profile_id,
            "day": u.day.isoformat(),
            "used_s": round(u.used_s),
            "extra_s": round(u.extra_s),
            "unlimited": u.unlimited,
            "blocked": u.blocked,
            "remaining_s": None if status.remaining_s is None else round(status.remaining_s),
            "can_start": status.can_start,
            "reason": status.reason.value if status.reason else None,
            "session_elapsed_s": None if status.session_elapsed_s is None else round(status.session_elapsed_s),
            # Watching means part of the episode on screen; the timer keeps the last group as its watchers.
            "watching": self.current is not None and profile_id in self.current.profile_ids,
        }

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=20)
        q.put_nowait(self.state())
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subscribers.discard(q)

    def _broadcast(self) -> None:
        if not self._subscribers:
            return
        snapshot = self.state()
        if snapshot == self._last_broadcast:
            return
        self._last_broadcast = snapshot
        for q in self._subscribers:
            if q.full():
                with contextlib.suppress(asyncio.QueueEmpty):
                    q.get_nowait()
            q.put_nowait(snapshot)
