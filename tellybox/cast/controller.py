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
from collections.abc import Awaitable, Callable, Collection
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from zoneinfo import ZoneInfo

from tellybox import library, media_urls, store
from tellybox.cast.device import (
    DEFAULT_MEDIA_RECEIVER,
    RECEIVER_LAUNCH_TIMEOUT_S,
    RECEIVER_RETRY_LAUNCH_TIMEOUT_S,
    CastDevice,
    ConnectionState,
    ConnectionStatus,
    DeviceEvent,
    DeviceInfo,
    LoadFailed,
    MediaStatus,
    PlayerState,
    ReceiverMessage,
    ReceiverStatus,
)
from tellybox.cast.pychromecast_device import CastCommandError, ReceiverUnavailable
from tellybox.clock import Clock
from tellybox.db import to_db
from tellybox.library import Episode
from tellybox.timer import Action, Activity, Decision, TimeUpReason, WatchTimer, day_for

log = logging.getLogger(__name__)

TICK_S = 1.0
CONNECT_WAIT_S = 20.0  # PB-6: how long a pick waits for a profile's TV to connect
PERSIST_EVERY_S = 15.0       # WT-8: at least every 30 s
STATUS_POLL_EVERY_S = 30.0   # drift check; the device sends nothing while playing
RECOVERY_WAIT_S = 10.0       # NF-7: how long to wait for our media to show up after a restart
RECONNECT_WAIT_S = 300.0     # PB-5: how long a lost connection may last before the session is ended
RESUME_TAIL_S = 10.0         # don't resume within the last seconds of an episode
RECEIVER_FALLBACK_S = 30 * 60.0  # CR-6: the longest stay on the Default Media Receiver (4th failed pick in a row, or a refusal)
RECEIVER_BACKOFF_S = (None, 5 * 60.0, 15 * 60.0, RECEIVER_FALLBACK_S)  # CR-6: fallback after the 1st, 2nd, 3rd, 4th+ failed pick
RECEIVER_RETRY_WAIT_S = 1.0  # CR-6: pause before the second launch attempt
RECEIVER_VANISH_SETTLE_S = 10.0  # CR-6: no app may be the step before another app (WT-9): a 1st-gen cold launch of e.g. YouTube takes seconds, and relaunching ours then would cut that cast off
BACKDROP_APP = "E8C28D3C"    # the Chromecast's idle screen
NIGHT_HOLD_S = 600.0         # CR-3: how long the night screen stays on the TV before the app quits
RECEIVER_PUSH_EVERY_S = 30.0  # CR-7: at least one state message this often while our receiver runs
FRACTION_STEP = 0.01         # CR-2: send a new state when the sky moves this much
LAST_FIVE_S = 300.0          # KA-8, as in the kid app


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
    # Receiver resilience (CR-6): when our receiver app vanished (no app or Backdrop) and we wait to see
    # whether it was a takeover; a vanished receiver is relaunched once per episode.
    vanished_at: datetime | None = None
    recovered: bool = False
    on_fallback: bool = False  # this episode plays on the Default Media Receiver because ours failed

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
        device_factory: Callable[[DeviceInfo], CastDevice] | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        self.conn = conn
        self._sleep = sleep or asyncio.sleep  # tests pass one that advances their fake clock
        self.clock = clock
        self.tz = tz
        self.media_base_url = media_base_url
        self.secret = secret
        self.device = device
        self.device_factory = device_factory  # PB-6: opens the connection to a profile's own TV
        self._connected = asyncio.Event()  # set when the current device reports CONNECTED
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
        # Tellybox receiver (v7). receiver_app_id follows the admin setting (each persist); None = Default Media Receiver only.
        self.receiver_app_id = store.receiver_app_id(conn)
        self.fallback_until: datetime | None = None  # CR-6
        self.receiver_error: str | None = None
        self.receiver_failures = 0  # CR-6: consecutive failed picks; any successful launch resets it
        self._receiver_refused = False  # the last failure was the device refusing our app
        self._load_lock = asyncio.Lock()  # one load at a time: a pick and a mid-episode relaunch don't interleave
        self._launching = False  # a load is in flight: receiver status events are ours to reconcile afterwards
        self._receiver_summary_cache: tuple[datetime, dict] | None = None
        self._rx_loading: dict | None = None  # CR-4: set from the pick until the episode plays
        self._rx_sent: dict | None = None
        self._rx_sent_at: datetime | None = None
        self._night_until: datetime | None = None  # CR-3
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
        self._connected.clear()
        self._night_until, self._rx_sent = None, None
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
        episode = library.get_episode(self.conn, episode_id)
        if episode is None:
            raise KeyError(episode_id)
        now = self.clock.now()
        self._refresh_profiles(now)
        self._apply_position_shifts(now)  # SB-3: resume from the remapped position
        if profile_ids is None:
            profiles = list(self._known_profiles)
            first = profiles[0] if profiles else None
        elif not profile_ids:
            raise ValueError("a pick needs at least one profile")
        else:
            profiles = sorted(set(profile_ids))
            first = list(dict.fromkeys(profile_ids))[0]  # PB-6: the first picker's TV wins
            if unknown := [p for p in profiles if p not in self._known_profiles]:
                raise UnknownProfile(unknown[0])
        target = store.target_device(self.conn, first) if first is not None else store.selected_device(self.conn)
        if self.device is None and (target is None or self.device_factory is None):
            raise NoDevice()
        decision = self.timer.on_pick(now, profiles)
        self._decision = decision
        if not decision.can_start:
            self._broadcast()
            raise PlayRefused(decision)  # a refused pick never moves the session to another TV
        await self._route_to(target)
        await self._start_episode(episode, now, profiles)

    async def _route_to(self, target: DeviceInfo | None) -> None:
        """PB-6: play on ``target``, the picking profile's TV. A different TV ends what plays on the old one
        (one session at a time), then connects before the pick loads."""
        if target is None or self.device_factory is None:
            return
        if self.device is not None and self.device.info.uuid == target.uuid:
            return
        log.info("switching to %s for this pick", target.name)
        await self.set_device(self.device_factory(target))
        try:
            await asyncio.wait_for(self._connected.wait(), CONNECT_WAIT_S)
        except TimeoutError:
            raise CastCommandError(f"{target.name} is not reachable") from None

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
        await self._push_receiver()
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
        await self._check_vanished(now)
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
        await self._end_night(now)
        await self._push_receiver()
        self._broadcast()

    async def _end_night(self, now: datetime) -> None:
        """CR-3: the night screen has been up long enough; quit our app so the TV can sleep."""
        if self._night_until is None:
            # Our receiver idle with nothing of ours to show: a restart or reconnect lost the night hold.
            # Only during a hold is it left idle, and it disables the TV's idle timeout, so start a fresh one.
            if self.current is None and self.connection == ConnectionState.CONNECTED and self._receiver_running():
                self._night_until = now + timedelta(seconds=NIGHT_HOLD_S)
                log.info("our receiver is idle with no night hold (restart or reconnect); quitting it at %s", self._night_until)
            return
        if now < self._night_until:
            return
        self._night_until = None
        if self.current is None and self.device and self._receiver_running():  # never quit somebody else's app (WT-9)
            with contextlib.suppress(CastCommandError):
                await self.device.stop()

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
            case ReceiverMessage(payload=payload):
                await self._on_receiver_message(payload)
        await self._push_receiver()
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
            self._connected.set()
            self._reconnected = self._lost_at is not None
            self._lost_at = None

    def _on_receiver(self, event: ReceiverStatus, now: datetime) -> None:
        reconnected, self._reconnected = self._reconnected, False
        c = self.current
        if c is None or c.stopping or self._launching:
            return  # while a load is in flight, _load adopts the session it ended up with
        if c.cast_session_id is None:
            if self._is_ours(event.app_id) and event.session_id:
                self._adopt_cast_session(c, event.session_id)
            return
        if event.session_id == c.cast_session_id:
            c.vanished_at = None  # our receiver is (back) on screen
            return
        latest = self.device.receiver if self.device else None
        if latest is not None and latest.session_id == c.cast_session_id:
            return  # a stale status: our session is the receiver's current one
        # Our receiver session is gone: someone cast something else, or the device rebooted (PB-5, WT-9).
        if reconnected:
            self._end_current(EndReason.DISCONNECTED)
        elif event.app_id in (None, BACKDROP_APP):
            # No app or the Backdrop: our receiver vanished, or it is the step before another app takes over.
            if latest is not None and latest.app_id not in (None, BACKDROP_APP):
                return  # the other app's status follows and decides
            if c.vanished_at is None:
                c.vanished_at = now
                c.position_s, c.position_at, c.player_state = c.position(now), now, PlayerState.UNKNOWN
                self._decision = self.timer.set_activity(now, Activity.STOPPED)  # nothing plays until it is back
        else:
            self._end_current(EndReason.TAKEN_OVER)

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
            if self._is_ours(r.app_id) and r.session_id:
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
            if state == PlayerState.PLAYING:
                self._rx_loading = None  # CR-4: the loading screen has done its job
        elif state == PlayerState.IDLE:
            if event.idle_reason == "FINISHED":
                await self._on_finished(now)
            elif event.idle_reason in ("CANCELLED", "INTERRUPTED"):
                if self._launching or c.vanished_at is not None:
                    return  # the receiver going away, or the media a relaunch replaces (CR-6)
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
            if not self._decision.can_start and await self._hold_night():
                return  # CR-3: time's up, so the TV keeps the night screen
            with contextlib.suppress(CastCommandError):
                await self.device.stop()  # back to the TV's idle screen

    async def _hold_night(self) -> bool:
        """CR-3: with our receiver on screen, time's up keeps the app up for NIGHT_HOLD_S instead of quitting it."""
        if not self._receiver_running():
            return False
        self._night_until = self.clock.now() + timedelta(seconds=NIGHT_HOLD_S)
        await self._push_receiver(force=True)  # time_up
        return True

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
        self._night_until = None  # a new load ends the night hold (CR-3)
        log.info("playing episode %s from %.0f s", episode.id, start_s)
        try:
            async with self._load_lock:
                await self._load(self.current, start_s)
        except CastCommandError:
            self._end_current(EndReason.LOAD_FAILED)
            raise
        self._broadcast()

    async def _load(self, c: Current, start_s: float) -> None:
        """Put `c.url` on the TV from `start_s`: on our receiver when it is configured and not backed off
        (two attempts, CR-6), else, or when both fail, on the Default Media Receiver."""
        assert self.device is not None
        now = self.clock.now()
        episode = c.episode
        app_id = self.receiver_app_id if self.receiver_app_id and not self._fallback_active(now) else None
        self._rx_loading = self._loading(episode) if app_id else None
        await self._push_receiver()  # CR-4: the loading screen goes up before the load
        c.on_fallback = False
        self._launching = True
        try:
            if app_id and not await self._launch_ours(c, app_id, start_s):
                app_id = None
                c.on_fallback = True
            if app_id is None:
                self._adopt_warm(c, DEFAULT_MEDIA_RECEIVER)
                await self.device.play(c.url, title=episode.title, start_s=start_s, app_id=None)
        finally:
            self._launching = False
        receiver = self.device.receiver  # the statuses that arrived meanwhile were skipped
        if c.cast_session_id is None and receiver and receiver.session_id and self._is_ours(receiver.app_id):
            self._adopt_cast_session(c, receiver.session_id)

    async def _launch_ours(self, c: Current, app_id: str, start_s: float) -> bool:
        """CR-6: load into the Tellybox receiver; one retry (after quitting a half-started app) unless the
        device refused it. False means both attempts failed and the pick falls back, with the next
        try after the backoff."""
        assert self.device is not None
        warm = self._receiver_running()
        failure: ReceiverUnavailable | None = None
        for attempt, timeout_s in enumerate((RECEIVER_LAUNCH_TIMEOUT_S, RECEIVER_RETRY_LAUNCH_TIMEOUT_S), 1):
            self._adopt_warm(c, app_id)
            started = self.clock.now()
            try:
                await self.device.play(c.url, title=c.episode.title, start_s=start_s, app_id=app_id,
                                       launch_timeout_s=timeout_s)
            except ReceiverUnavailable as exc:
                failure = exc
                self._record("refused" if exc.refused else "launch_failed", f"attempt {attempt}: {exc}",
                             duration_ms=self._ms_since(started), episode_id=c.episode.id)
                if exc.refused or attempt == 2:
                    break
                log.warning("Tellybox receiver did not start (%s); trying once more", exc)
                await self._quit_half_started(app_id)
                await self._sleep(RECEIVER_RETRY_WAIT_S)
                continue
            self._record("launch_ok", f"attempt {attempt}" + (", warm" if warm else ""),
                         duration_ms=self._ms_since(started), episode_id=c.episode.id)
            self.receiver_error, self.receiver_failures, self._receiver_refused = None, 0, False
            self.fallback_until = None
            return True
        assert failure is not None
        now = self.clock.now()
        self.receiver_error = str(failure)
        self.receiver_failures += 1
        self._receiver_refused = failure.refused
        delay = RECEIVER_FALLBACK_S if failure.refused else RECEIVER_BACKOFF_S[min(self.receiver_failures, 4) - 1]
        self.fallback_until = now + timedelta(seconds=delay) if delay else None
        self._rx_loading = None
        self._record("fallback", f"until {to_db(self.fallback_until)}" if self.fallback_until else "this episode",
                     episode_id=c.episode.id)
        log.warning("Tellybox receiver unavailable (%s); using the Default Media Receiver %s", failure,
                    f"until {self.fallback_until}" if self.fallback_until else "for this episode")
        return False

    async def _quit_half_started(self, app_id: str) -> None:
        """A launch that timed out may have left our app half-started; clear it before the second attempt."""
        assert self.device is not None
        receiver = self.device.receiver
        if receiver is not None and receiver.app_id == app_id:
            with contextlib.suppress(CastCommandError):
                await self.device.quit_app()

    def _ms_since(self, started: datetime) -> int:
        return round((self.clock.now() - started).total_seconds() * 1000)

    def _record(self, kind: str, detail: str | None = None, *, duration_ms: int | None = None,
                episode_id: int | None = None) -> None:
        """Log a receiver event (CR-6). The summary behind the cast state is recomputed."""
        store.record_receiver_event(self.conn, self.clock.now(), kind, detail, duration_ms=duration_ms,
                                    episode_id=episode_id)
        self._receiver_summary_cache = None

    async def _check_vanished(self, now: datetime) -> None:
        """CR-6: our receiver app vanished mid-episode (no app or the Backdrop, and no other app followed within
        the settle time). Relaunch once per episode at the estimated position, keeping the watch session,
        while the timer allows playback. Anything else ends the episode as a take-over, as before (WT-9)."""
        c = self.current
        if c is None or c.vanished_at is None or c.stopping or self.device is None:
            return
        if (now - c.vanished_at).total_seconds() < RECEIVER_VANISH_SETTLE_S:
            return
        receiver = self.device.receiver
        if receiver is not None and receiver.session_id == c.cast_session_id:
            c.vanished_at = None
            return
        if receiver is not None and receiver.app_id not in (None, BACKDROP_APP):
            return  # another app: its status event ends the episode (WT-9)
        pos = c.position_s
        self._record("lost", f"{receiver.app_id if receiver and receiver.app_id else 'no app'} at {pos:.0f} s",
                     episode_id=c.episode.id)
        near_end = bool(c.duration_s) and pos >= c.duration_s - RESUME_TAIL_S
        if (
            c.recovered
            or near_end
            or not self._decision.can_start
            or self.connection != ConnectionState.CONNECTED
            or self._lost_at is not None
        ):
            self._end_current(EndReason.TAKEN_OVER)
            return
        c.recovered, c.vanished_at = True, None
        c.cast_session_id, c.media_session_id, c.replaced_media_session_id = None, None, None
        c.position_at = now
        log.info("our receiver vanished; relaunching episode %s at %.0f s", c.episode.id, pos)
        try:
            async with self._load_lock:
                if self.current is not c or c.stopping:
                    return  # a pick or a stop came first
                await self._load(c, pos)
        except CastCommandError as exc:
            self._record("recover_failed", str(exc), episode_id=c.episode.id)
            if self.current is c:
                self._end_current(EndReason.TAKEN_OVER)
            return
        self._record("recovered", f"at {pos:.0f} s on {'the Default Media Receiver' if c.on_fallback else 'our receiver'}",
                     episode_id=c.episode.id)

    def _adopt_warm(self, c: Current, app_id: str) -> None:
        """A receiver that already runs `app_id` sends no new session event when we load into it."""
        c.cast_session_id, c.replaced_media_session_id = None, None
        receiver = self.device.receiver if self.device else None
        if receiver and receiver.session_id and self._is_ours(receiver.app_id):
            if receiver.app_id == app_id:
                self._adopt_cast_session(c, receiver.session_id)
            # Also when we move between our two receivers: the media left behind may report late.
            if self.device.media is not None:
                c.replaced_media_session_id = self.device.media.media_session_id

    def _is_ours(self, app_id: str | None) -> bool:
        """WT-9: the receiver app that plays our media is the Default Media Receiver or the Tellybox receiver."""
        return app_id is not None and (app_id == DEFAULT_MEDIA_RECEIVER or app_id == self.receiver_app_id)

    def _fallback_active(self, now: datetime) -> bool:
        return self.fallback_until is not None and now < self.fallback_until

    def _receiver_running(self) -> bool:
        r = self.device.receiver if self.device else None
        return bool(self.receiver_app_id and r and r.app_id == self.receiver_app_id)

    def _adopt_cast_session(self, c: Current, cast_session_id: str) -> None:
        c.cast_session_id = cast_session_id
        store.set_cast_session_id(self.conn, c.watch_session_id, cast_session_id)

    async def _stop_current(self, reason: EndReason) -> None:
        c = self.current
        if c is None:
            return
        c.stopping = True
        night = reason in (EndReason.TIME_UP, EndReason.BLOCKED) or (
            reason == EndReason.PARENT_STOP and not self._decision.can_start
        )
        held = False
        if self.device:
            try:
                if night and self._receiver_running():
                    await self.device.stop_media()  # CR-3: the app stays up and shows the night
                    held = True
                else:
                    await self.device.stop()
            except CastCommandError:
                log.warning("stop command failed; ending the session anyway")
        self._end_current(reason)
        if held:
            await self._hold_night()

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
        self._rx_loading = None
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
                cast_session_id=receiver.session_id if receiver and self._is_ours(receiver.app_id) else rs.cast_session_id,
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
        if force:
            self.receiver_app_id = store.receiver_app_id(self.conn)
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
            "receiver": self._receiver_state(now),
        }

    def _receiver_state(self, now: datetime) -> dict:
        fallback = self._fallback_active(now)
        on_default = fallback or (self.current is not None and self.current.on_fallback)
        cached = self._receiver_summary_cache
        if cached is None or (now - cached[0]).total_seconds() >= 60:  # the 24 h window moves on
            cached = self._receiver_summary_cache = (now, store.receiver_summary(self.conn, now))
        return {
            "kind": "tellybox" if self.receiver_app_id and not on_default else "default",
            "configured": self.receiver_app_id is not None,
            "fallback_until": to_db(self.fallback_until) if fallback else None,
            "last_error": self.receiver_error,
            **cached[1],
            "refused": self._receiver_refused and fallback,
        }

    # ------------------------------------------------------------------ Tellybox receiver messages (CR-2..CR-8)

    async def _on_receiver_message(self, payload: dict) -> None:
        match payload.get("type"):
            case "hello":
                log.info("receiver connected: %s (sdk attempts %s, loaded in %s ms)",
                         payload.get("ua"), payload.get("sdk_attempts"), payload.get("load_ms"))
                attempts = payload.get("sdk_attempts")
                if isinstance(attempts, int) and attempts > 1:  # the page needed retries to load the Cast SDK
                    self._record("page_error", f"Cast SDK loaded after {attempts} attempts ({payload.get('load_ms')} ms)")
                await self._push_receiver(force=True)  # a receiver that just (re)started needs the whole state
            case "stats":  # CR-8
                log.info("receiver stats: dropped=%s total=%s state=%s",
                         payload.get("dropped"), payload.get("total"), payload.get("state"))
            case "log":
                level = logging.ERROR if payload.get("level") == "error" else logging.INFO
                log.log(level, "receiver: %s", payload.get("msg"))
                if level == logging.ERROR:
                    self._record("page_error", str(payload.get("msg"))[:300])
            case other:
                log.debug("unknown receiver message %r", other)

    def _loading(self, episode: Episode) -> dict:
        base = self.media_base_url.rstrip("/")
        show = library.get_show(self.conn, episode.show_id)
        return {
            "artwork": f"{base}/img/show/{episode.show_id}.jpg" if show and show.artwork_path else None,
            "thumb": f"{base}/img/episode/{episode.id}.jpg",
        }

    def _up_next(self) -> dict | None:
        """CR-5: the next thumbnail, only when autoplay will actually continue."""
        c = self.current
        if c is None or c.stopping or not self._decision.autoplay_allowed:
            return None
        show = library.get_show(self.conn, c.episode.show_id)
        nxt = library.next_episode(self.conn, c.episode.id) if show and show.autoplay else None
        return {"thumb": f"{self.media_base_url.rstrip('/')}/img/episode/{nxt.id}.jpg"} if nxt else None

    def _sky(self, now: datetime) -> dict:
        """The watchers' sky, as the kid app's KidState.sky (tellybox/web/kid.py::_fraction_left)."""
        remaining = self._decision.remaining_s
        if remaining is None:
            return {"fraction_left": None, "last_five": False, "unlimited": True}
        allowance = {
            r["id"]: r["daily_allowance_min"] * 60.0
            for r in self.conn.execute("SELECT id, daily_allowance_min FROM profile ORDER BY id")
        }
        entries = [self._profile_state(now, p) for p in self._known_profiles if p in allowance]
        watchers = [p for p in entries if p["watching"]]
        limited = [p for p in (watchers or entries) if not p["unlimited"]]

        def fraction(left: float, total: float) -> float:
            return min(max(left / total, 0.0), 1.0) if total > 0 else 0.0

        if len(limited) > 1:  # several profiles watching; the one with the least left decides
            left = min(fraction(p["remaining_s"], allowance[p["profile_id"]] + p["extra_s"]) for p in limited)
        elif limited:
            left = fraction(remaining, allowance[limited[0]["profile_id"]] + limited[0]["extra_s"])
        else:
            left = fraction(remaining, allowance.get(self._known_profiles[0], 0.0) if self._known_profiles else 0.0)
        return {"fraction_left": round(left, 3), "last_five": remaining <= LAST_FIVE_S, "unlimited": False}

    def _receiver_message(self, now: datetime) -> dict:
        return {
            "type": "state", "v": 1,
            "sky": self._sky(now),
            "time_up": not self._decision.can_start,
            "loading": self._rx_loading,
            "up_next": self._up_next(),
        }

    def _rx_due(self, msg: dict, now: datetime) -> bool:
        last = self._rx_sent
        if last is None or self._rx_sent_at is None:
            return True
        if any(last[k] != msg[k] for k in ("time_up", "loading", "up_next")):
            return True
        a, b = last["sky"], msg["sky"]
        if a["last_five"] != b["last_five"] or a["unlimited"] != b["unlimited"]:
            return True
        if (a["fraction_left"] is None) != (b["fraction_left"] is None):
            return True
        if a["fraction_left"] is not None and abs(a["fraction_left"] - b["fraction_left"]) >= FRACTION_STEP - 1e-9:
            return True
        return (now - self._rx_sent_at).total_seconds() >= RECEIVER_PUSH_EVERY_S

    async def _push_receiver(self, force: bool = False) -> None:
        """Send the receiver its state when something it shows changed (CR-2, CR-7).
        `force` (a hello, a load, time up) skips the throttle, and for a hello also the running check."""
        if self.device is None or not self.receiver_app_id:
            self._rx_sent = None  # no receiver configured: exactly the v1 behaviour, nothing is sent
            return
        if not force and not self._receiver_running():
            self._rx_sent = None  # the next time it runs it needs the whole state
            return
        now = self.clock.now()
        msg = self._receiver_message(now)
        if not force and not self._rx_due(msg, now):
            return
        self._rx_sent, self._rx_sent_at = msg, now
        with contextlib.suppress(CastCommandError):
            await self.device.send_receiver_message(msg)

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
