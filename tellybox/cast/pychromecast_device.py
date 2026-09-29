"""`CastDevice` backed by pychromecast (the real Chromecast).

pychromecast runs its own socket thread and calls our listeners from it; the
listeners translate its objects into the frozen dataclasses from `device.py`
and hand them to the asyncio loop with `call_soon_threadsafe`. Blocking
pychromecast calls run in `asyncio.to_thread`.

After a connection loss pychromecast keeps reconnecting on its own (spike:
~39 s after a power cycle); we only surface the ConnectionStatus events.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
from collections.abc import AsyncIterator, Callable
from typing import Any
from uuid import UUID

import pychromecast
import pychromecast.discovery
from pychromecast.error import PyChromecastError, RequestFailed

from tellybox.cast.device import (
    ConnectionState,
    ConnectionStatus,
    DeviceEvent,
    DeviceInfo,
    LoadFailed,
    MediaStatus,
    PlayerState,
    ReceiverStatus,
)

log = logging.getLogger(__name__)

DIRECT_CONNECT_TIMEOUT = 5.0  # a reachable Chromecast answers well within this; then try mDNS
_CONNECTION_STATES = {s.value: s for s in ConnectionState} | {"FAILED_RESOLVE": ConnectionState.FAILED}


class CastCommandError(Exception):
    """A command could not be delivered to the Chromecast (not connected, timeout, refused)."""


class ReceiverUnavailable(CastCommandError):
    """The Tellybox receiver could not be launched; the caller falls back to the Default Media Receiver (CR-6)."""


async def discover(timeout: float = 8.0, known_hosts: list[str] | None = None) -> list[DeviceInfo]:
    def run() -> list[DeviceInfo]:
        infos, browser = pychromecast.discovery.discover_chromecasts(timeout=timeout, known_hosts=known_hosts)
        browser.stop_discovery()
        return [DeviceInfo(str(i.uuid), i.friendly_name, i.host, i.port, i.model_name) for i in infos]

    return await asyncio.to_thread(run)


def _player_state(value: str | None) -> PlayerState:
    try:
        return PlayerState(value)
    except ValueError:
        return PlayerState.UNKNOWN


class _Listener:
    """Receives pychromecast callbacks (socket thread) and forwards translated events."""

    def __init__(self, post: Callable[[DeviceEvent], None]) -> None:
        self.post = post
        self.active = True

    def _send(self, event: DeviceEvent) -> None:
        if self.active:
            self.post(event)

    def new_cast_status(self, status: Any) -> None:
        self._send(ReceiverStatus(status.app_id, status.session_id, status.display_name))

    def new_media_status(self, status: Any) -> None:
        self._send(
            MediaStatus(
                _player_state(status.player_state),
                status.content_id,
                float(status.current_time or 0.0),
                status.duration,
                status.idle_reason,
                status.media_session_id,
            )
        )

    def load_media_failed(self, queue_item_id: int, error_code: int) -> None:
        self._send(LoadFailed(error_code))

    def new_launch_error(self, status: Any) -> None:
        log.warning("cast launch failed: %s", status)
        self._send(LoadFailed(None))

    def new_connection_status(self, status: Any) -> None:
        state = _CONNECTION_STATES.get(status.status)
        if state is None:
            log.warning("unknown cast connection status %r", status.status)
            return
        self._send(ConnectionStatus(state))


class PyChromecastDevice:
    def __init__(self, info: DeviceInfo, *, discovery_fallback: bool = True) -> None:
        self.info: DeviceInfo | None = info
        self.discovery_fallback = discovery_fallback
        self._cast: Any = None
        self._browser: Any = None
        self._listener: _Listener | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._queue: asyncio.Queue[DeviceEvent] | None = None
        self._receiver: ReceiverStatus | None = None
        self._media: MediaStatus | None = None

    @property
    def receiver(self) -> ReceiverStatus | None:
        return self._receiver

    @property
    def media(self) -> MediaStatus | None:
        return self._media

    # ------------------------------------------------------------------ events

    def _post(self, event: DeviceEvent) -> None:  # socket thread
        loop = self._loop
        if loop is None:
            return
        try:
            loop.call_soon_threadsafe(self._deliver, event)
        except RuntimeError:  # loop closed during shutdown
            pass

    def _deliver(self, event: DeviceEvent) -> None:  # loop thread
        if isinstance(event, ReceiverStatus):
            self._receiver = event
        elif isinstance(event, MediaStatus):
            self._media = event
        assert self._queue is not None
        self._queue.put_nowait(event)

    async def events(self) -> AsyncIterator[DeviceEvent]:
        if self._queue is None:
            self._queue = asyncio.Queue()
        while True:
            yield await self._queue.get()

    # ------------------------------------------------------------------ connection

    async def connect(self, timeout: float = 15.0) -> None:
        self._loop = asyncio.get_running_loop()
        if self._queue is None:
            self._queue = asyncio.Queue()
        await asyncio.to_thread(self._connect, timeout)

    def _attach(self, cast: Any) -> None:
        if self._listener is not None:
            self._listener.active = False
        self._listener = listener = _Listener(self._post)
        cast.register_status_listener(listener)
        cast.register_connection_listener(listener)
        cast.register_launch_error_listener(listener)
        cast.media_controller.register_status_listener(listener)
        self._cast = cast

    def _detach(self) -> None:
        if self._listener is not None:
            self._listener.active = False
        cast, self._cast = self._cast, None
        if cast is not None:
            try:
                cast.disconnect(timeout=0)
            except Exception:  # noqa: BLE001 - best effort
                log.debug("disconnect failed", exc_info=True)

    def _connect(self, timeout: float) -> None:  # worker thread
        info = self.info
        assert info is not None
        cast = pychromecast.get_chromecast_from_host((info.host, info.port, UUID(info.uuid), info.model, info.name))
        self._attach(cast)
        direct_timeout = min(timeout, DIRECT_CONNECT_TIMEOUT) if self.discovery_fallback else timeout
        try:
            cast.wait(direct_timeout)
        except PyChromecastError as exc:
            self._detach()
            if not self.discovery_fallback:
                raise CastCommandError(f"connect {info.host}: {exc!r}") from exc
            log.warning("cast not reachable at %s (%r); discovering by uuid", info.host, exc)
            cast = self._discover_cast(info, timeout)
            self._attach(cast)
            try:
                cast.wait(timeout)
            except PyChromecastError as exc2:
                self._detach()
                raise CastCommandError(f"connect {info.uuid}: {exc2!r}") from exc2
        ci = cast.cast_info
        self.info = dataclasses.replace(
            info, host=ci.host, port=ci.port or info.port, name=ci.friendly_name or info.name, model=ci.model_name or info.model
        )

    def _discover_cast(self, info: DeviceInfo, timeout: float) -> Any:
        self._stop_browser()
        casts, browser = pychromecast.get_listed_chromecasts(
            uuids=[UUID(info.uuid)], known_hosts=[info.host], discovery_timeout=timeout
        )
        if not casts:
            browser.stop_discovery()
            raise CastCommandError(f"Chromecast {info.uuid} not found")
        self._browser = browser  # keeps mDNS data fresh for pychromecast's reconnects
        return casts[0]

    def _stop_browser(self) -> None:
        browser, self._browser = self._browser, None
        if browser is not None:
            browser.stop_discovery()

    async def close(self) -> None:
        cast = self._cast
        if self._listener is not None:
            self._listener.active = False
        self._cast = None
        if cast is not None:
            try:
                await asyncio.to_thread(cast.disconnect, timeout=5)
            except Exception:  # noqa: BLE001 - best effort
                log.warning("cast disconnect failed", exc_info=True)
        await asyncio.to_thread(self._stop_browser)

    # ------------------------------------------------------------------ commands

    async def _run(self, name: str, fn: Callable[[Any], None]) -> None:
        cast = self._cast
        if cast is None:
            raise CastCommandError(f"{name}: not connected")
        try:
            await asyncio.to_thread(fn, cast)
        except PyChromecastError as exc:
            log.warning("cast command %s failed: %r", name, exc)
            raise CastCommandError(f"{name}: {exc!r}") from exc

    async def play(self, url: str, *, title: str | None = None, start_s: float = 0.0, app_id: str | None = None) -> None:
        if app_id is not None:
            raise NotImplementedError  # step 13, part A
        # BUFFERED: pychromecast defaults to LIVE, which disables seeking (spike).
        await self._run(
            "play",
            lambda c: c.media_controller.play_media(
                url, "video/mp4", title=title, stream_type="BUFFERED", current_time=start_s or None
            ),
        )

    async def send_receiver_message(self, payload: dict) -> None:
        raise NotImplementedError  # step 13, part A

    async def pause(self) -> None:
        await self._run("pause", lambda c: c.media_controller.pause())

    async def resume(self) -> None:
        await self._run("resume", lambda c: c.media_controller.play())

    async def request_status(self) -> None:
        await self._run("request_status", lambda c: c.media_controller.update_status())

    async def stop(self) -> None:
        def run(c: Any) -> None:
            try:
                c.media_controller.stop()
            except PyChromecastError as exc:  # e.g. no media session; quitting the app still works
                log.info("media stop failed (%r); quitting app anyway", exc)
            try:
                c.quit_app()
            except RequestFailed as exc:  # nothing to quit
                log.info("quit_app refused: %r", exc)

        await self._run("stop", run)
