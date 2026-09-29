"""In-memory Chromecast for controller and timer tests.

Deterministic: events are enqueued synchronously, positions come from a
`FakeClock`. Mirrors the real-device behaviour found in the spike
(docs/spike-casting.md): position 0 on FINISHED, a null receiver session
before a foreign app starts, Backdrop after a power cycle.

While disconnected, state changes still happen but no events are delivered;
`reconnect()` reports the current state, as pychromecast does.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass

from tellybox.cast.device import (
    DEFAULT_MEDIA_RECEIVER,
    ConnectionState,
    ConnectionStatus,
    DeviceEvent,
    DeviceInfo,
    MediaStatus,
    PlayerState,
    ReceiverMessage,
    ReceiverStatus,
)
from tellybox.cast.pychromecast_device import CastCommandError, ReceiverUnavailable
from tellybox.clock import FakeClock

BACKDROP_APP = "E8C28D3C"
YOUTUBE_APP = "233637DE"


@dataclass
class _Media:
    url: str
    state: PlayerState
    pos: float  # position at `since`
    since: float  # clock timestamp of the last state change
    duration: float | None
    idle_reason: str | None = None
    session_id: int | None = None


class FakeCastDevice:
    def __init__(self, clock: FakeClock, info: DeviceInfo | None = None, default_duration_s: float = 600.0) -> None:
        self.clock = clock
        self.info = info or DeviceInfo(uuid="00000000-0000-0000-0000-000000000001", name="Fake TV", host="127.0.0.1")
        self.default_duration_s = default_duration_s
        self.durations: dict[str, float] = {}
        self.calls: list[tuple] = []
        self.connected = False
        self.fail_next_command = False
        # Tellybox receiver (v7): launch failure injection, the app id we launched, what the cast service sent.
        self.fail_launch = False
        self.play_app_ids: list[str | None] = []
        self.sent_messages: list[dict] = []
        self._tellybox_app_id: str | None = None
        self._queue: asyncio.Queue[DeviceEvent] | None = None
        self._receiver: ReceiverStatus | None = None
        self._media_status: MediaStatus | None = None
        self._n = {"backdrop": 1, "dmr": 0, "tellybox": 0, "foreign": 0, "media": 0}
        self._app = ReceiverStatus(BACKDROP_APP, "backdrop-1", "Backdrop")
        self._m: _Media | None = None
        self._frozen = False

    # ------------------------------------------------------------------ internals

    @property
    def queue(self) -> asyncio.Queue[DeviceEvent]:
        if self._queue is None:
            self._queue = asyncio.Queue()
        return self._queue

    def _t(self) -> float:
        return self.clock.now().timestamp()

    def _emit(self, event: DeviceEvent, force: bool = False) -> None:
        if not (self.connected or force):
            return
        if isinstance(event, ReceiverStatus):
            self._receiver = event
        elif isinstance(event, MediaStatus):
            self._media_status = event
        self.queue.put_nowait(event)

    def _status(self) -> MediaStatus | None:
        m = self._m
        if m is None:
            return None
        pos = 0.0 if m.state == PlayerState.IDLE else self.position()
        return MediaStatus(m.state, m.url, pos, m.duration, m.idle_reason, m.session_id)

    def _emit_media(self) -> None:
        if (s := self._status()) is not None:
            self._emit(s)

    def _set_state(self, state: PlayerState, idle_reason: str | None = None) -> None:
        m = self._m
        assert m is not None
        m.pos, m.since, m.state, m.idle_reason = self.position(), self._t(), state, idle_reason
        if state == PlayerState.IDLE:
            m.pos = 0.0
        self._emit_media()

    def _set_app(self, kind: str, app_id: str, name: str) -> None:
        self._n[kind] += 1
        self._app = ReceiverStatus(app_id, f"{kind}-{self._n[kind]}", name)

    def _backdrop(self) -> None:
        self._set_app("backdrop", BACKDROP_APP, "Backdrop")

    def _duration(self, url: str) -> float:
        return next((d for k, d in self.durations.items() if k in url), self.default_duration_s)

    def _command(self, *call: object) -> None:
        self.calls.append(call)
        if self.fail_next_command:
            self.fail_next_command = False
            raise CastCommandError(f"{call[0]}: injected failure")
        if not self.connected:
            raise CastCommandError(f"{call[0]}: not connected")

    def _active(self, cmd: str) -> _Media:
        if self._m is None or self._m.state == PlayerState.IDLE:
            raise CastCommandError(f"{cmd}: no active media session")
        return self._m

    # ------------------------------------------------------------------ CastDevice

    @property
    def receiver(self) -> ReceiverStatus | None:
        return self._receiver

    @property
    def media(self) -> MediaStatus | None:
        return self._media_status

    def position(self) -> float:
        m = self._m
        if m is None:
            return 0.0
        pos = m.pos
        if m.state == PlayerState.PLAYING and not self._frozen:
            pos += self._t() - m.since
        return min(pos, m.duration) if m.duration is not None else pos

    async def connect(self, timeout: float = 15.0) -> None:
        self.calls.append(("connect",))
        self.reconnect()

    async def close(self) -> None:
        self.calls.append(("close",))
        self.connected = False

    async def events(self) -> AsyncIterator[DeviceEvent]:
        while True:
            yield await self.queue.get()

    async def play(self, url: str, *, title: str | None = None, start_s: float = 0.0, app_id: str | None = None) -> None:
        self._command("play", url, start_s)
        self.play_app_ids.append(app_id)
        if app_id is not None:
            if self._app.app_id != app_id:
                if self.fail_launch:
                    raise ReceiverUnavailable("launch timed out")
                self._tellybox_app_id = app_id
                self._set_app("tellybox", app_id, "Tellybox")
                self._emit(self._app)
                self.hello()
        elif self._app.app_id != DEFAULT_MEDIA_RECEIVER:
            self._set_app("dmr", DEFAULT_MEDIA_RECEIVER, "Default Media Receiver")
            self._emit(self._app)
        replaced = self._m if self._m is not None and self._m.state != PlayerState.IDLE else None
        self._n["media"] += 1
        sid, duration = self._n["media"], self._duration(url)
        self._emit(MediaStatus(PlayerState.IDLE, url, 0.0, None, media_session_id=sid))
        if replaced is not None:
            # The receiver reports the replaced media as interrupted. That message has no media
            # field, and pychromecast keeps the last contentId it saw, so the status carries the
            # *new* URL with the *old* media session id (seen on the real TV, 2026-09-28).
            self._emit(MediaStatus(PlayerState.IDLE, url, 0.0, None, "INTERRUPTED", replaced.session_id))
        self._m = _Media(url, PlayerState.BUFFERING, start_s, self._t(), duration, session_id=sid)
        self._emit_media()
        self._set_state(PlayerState.PLAYING)

    async def pause(self) -> None:
        self._command("pause")
        self._active("pause")
        self._set_state(PlayerState.PAUSED)

    async def resume(self) -> None:
        self._command("resume")
        self._active("resume")
        self._set_state(PlayerState.PLAYING)

    async def send_receiver_message(self, payload: dict) -> None:
        if self.receiver_running:  # like the real device: nothing reaches a receiver that isn't ours
            self.sent_messages.append(payload)
            self.calls.append(("receiver_message", payload))

    @property
    def receiver_running(self) -> bool:
        return self._tellybox_app_id is not None and self._app.app_id == self._tellybox_app_id

    async def stop_media(self) -> None:
        self._command("stop_media")
        if self._m is not None and self._m.state != PlayerState.IDLE:
            self._set_state(PlayerState.IDLE, "CANCELLED")
        self._m = None

    async def stop(self) -> None:
        self._command("stop")
        if self._m is not None and self._m.state != PlayerState.IDLE:
            self._set_state(PlayerState.IDLE, "CANCELLED")
        self._m = None
        self._backdrop()
        self._emit(self._app)

    async def request_status(self) -> None:
        self._command("request_status")
        self._emit_media()

    # ------------------------------------------------------------------ test helpers

    def hello(self) -> None:
        """The Tellybox receiver reports a connected sender (docs/receiver-protocol.md)."""
        self._emit(ReceiverMessage({"type": "hello", "v": 1, "ua": "FakeReceiver/1.0"}))

    def receiver_message(self, payload: dict) -> None:
        self._emit(ReceiverMessage(payload))

    def finish(self) -> None:
        self._set_state(PlayerState.IDLE, "FINISHED")

    def external_pause(self) -> None:
        self._set_state(PlayerState.PAUSED)

    def external_resume(self) -> None:
        self._set_state(PlayerState.PLAYING)

    def buffer(self) -> None:
        self._set_state(PlayerState.BUFFERING)

    def takeover(self, app_id: str = YOUTUBE_APP, content_id: str = "yt-abc123") -> None:
        self._emit(ReceiverStatus(None, None))
        self._set_app("foreign", app_id, "YouTube")
        self._emit(self._app)
        self._m = _Media(content_id, PlayerState.PLAYING, 0.0, self._t(), 300.0)
        self._emit_media()

    def end_foreign(self) -> None:
        self._m = None
        self._backdrop()
        self._emit(self._app)

    def lose_connection(self) -> None:
        if self._m is not None:
            self._m.pos, self._m.since = self.position(), self._t()
        self._frozen, self.connected = True, False
        self._emit(ConnectionStatus(ConnectionState.LOST), force=True)

    def power_cycle(self) -> None:
        self.lose_connection()
        self._m = None
        self._backdrop()

    def reconnect(self) -> None:
        if self._frozen and self._m is not None:
            self._m.since = self._t()
        self._frozen, self.connected = False, True
        self._emit(ConnectionStatus(ConnectionState.CONNECTED))
        self._emit(self._app)
        self._emit_media()

    def preload(
        self,
        url: str,
        position_s: float,
        state: PlayerState = PlayerState.PLAYING,
        duration_s: float | None = None,
        app_id: str | None = None,
    ) -> None:
        if app_id is not None:  # already playing in the Tellybox receiver
            self._tellybox_app_id = app_id
            self._set_app("tellybox", app_id, "Tellybox")
        else:
            self._set_app("dmr", DEFAULT_MEDIA_RECEIVER, "Default Media Receiver")
        self._n["media"] += 1
        duration = duration_s if duration_s is not None else self._duration(url)
        self._m = _Media(url, state, position_s, self._t(), duration, session_id=self._n["media"])

    def drain(self) -> list[DeviceEvent]:
        out = []
        while not self.queue.empty():
            out.append(self.queue.get_nowait())
        return out
