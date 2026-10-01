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
import time
from collections.abc import AsyncIterator, Callable
from typing import Any
from uuid import UUID

import pychromecast
import pychromecast.discovery
from pychromecast.controllers import BaseController
from pychromecast.error import NotConnected, PyChromecastError, RequestFailed

from tellybox.cast.device import (
    ConnectionState,
    ConnectionStatus,
    DEFAULT_MEDIA_RECEIVER,
    DeviceEvent,
    DeviceInfo,
    LoadFailed,
    MediaStatus,
    PlayerState,
    ReceiverMessage,
    ReceiverStatus,
    RECEIVER_LAUNCH_TIMEOUT_S,
    RECEIVER_NAMESPACE,
)

log = logging.getLogger(__name__)

DIRECT_CONNECT_TIMEOUT = 5.0  # a reachable Chromecast answers well within this; then try mDNS
_CONNECTION_STATES = {s.value: s for s in ConnectionState} | {"FAILED_RESOLVE": ConnectionState.FAILED}


class CastCommandError(Exception):
    """A command could not be delivered to the Chromecast (not connected, timeout, refused)."""


class ReceiverUnavailable(CastCommandError):
    """The Tellybox receiver could not be launched; the caller falls back to the Default Media Receiver (CR-6).
    `refused` means the device rejected the app outright (an unregistered app id fails at once, spike S8),
    so another attempt is pointless; a timeout or any other error is not a refusal."""

    def __init__(self, message: str, *, refused: bool = False) -> None:
        super().__init__(message)
        self.refused = refused


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


class TellyboxController(BaseController):
    """The custom namespace to the Tellybox receiver (docs/receiver-protocol.md). It has no supporting app id:
    it never launches anything, so a message can't replace whatever app runs."""

    def __init__(self, post: Callable[[DeviceEvent], None]) -> None:
        super().__init__(RECEIVER_NAMESPACE)
        self._post = post

    def receive_message(self, _message: Any, data: dict) -> bool:  # socket thread
        if isinstance(data, dict):
            self._post(ReceiverMessage(data))
        return True


class _Listener:
    """Receives pychromecast callbacks (socket thread) and forwards translated events."""

    def __init__(self, post: Callable[[DeviceEvent], None]) -> None:
        self.post = post
        self.active = True
        self.launching = False  # a Tellybox receiver launch is in flight: its errors are ours, not a failed load
        self.launch_error: str | None = None

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
        if self.launching:
            self.launch_error = getattr(status, "reason", None) or "launch error"
            return
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
        self._tellybox: TellyboxController | None = None

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
        self._tellybox = TellyboxController(listener._send)
        cast.register_handler(self._tellybox)
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

    async def play(
        self,
        url: str,
        *,
        title: str | None = None,
        start_s: float = 0.0,
        app_id: str | None = None,
        launch_timeout_s: float = RECEIVER_LAUNCH_TIMEOUT_S,
    ) -> None:
        def run(c: Any) -> None:
            if app_id is not None:
                self._launch_receiver(c, app_id, launch_timeout_s)
            elif c.app_id not in (None, DEFAULT_MEDIA_RECEIVER) and c.media_controller.is_active:
                # The media controller loads into any running app that speaks the media namespace (e.g. the
                # Tellybox receiver after its app id was cleared); our media belongs in the DMR (PB-2, WT-9).
                c.start_app(DEFAULT_MEDIA_RECEIVER, timeout=RECEIVER_LAUNCH_TIMEOUT_S)
            # BUFFERED: pychromecast defaults to LIVE, which disables seeking (spike).
            c.media_controller.play_media(
                url, "video/mp4", title=title, stream_type="BUFFERED", current_time=start_s or None
            )

        await self._run("play", run)

    def _launch_receiver(self, cast: Any, app_id: str, timeout_s: float = RECEIVER_LAUNCH_TIMEOUT_S) -> None:  # worker thread
        """Start the Tellybox receiver unless it runs, and wait until it takes media (CR-1, CR-6).
        The media controller would launch the Default Media Receiver over a receiver that hasn't registered
        the media namespace yet, so 'running' means the app id matches *and* the namespace is there.
        On the 1st gen a cold launch takes ~3 s, and an unregistered app id fails at once with RequestFailed
        (docs/spike-receiver.md, S1 and S8). A launch error of CANCELLED (seen at rollout, until a reboot) and a
        RequestFailed are refusals: the device won't take the app, and a retry can't help."""

        def ready() -> bool:
            return cast.app_id == app_id and cast.media_controller.is_active

        if ready():
            return
        listener = self._listener
        assert listener is not None
        listener.launching, listener.launch_error = True, None
        deadline = time.monotonic() + timeout_s
        try:
            cast.start_app(app_id, timeout=timeout_s)
            while not ready():
                if listener.launch_error:
                    raise ReceiverUnavailable(
                        f"launch failed: {listener.launch_error}", refused=listener.launch_error == "CANCELLED"
                    )
                if time.monotonic() >= deadline:
                    raise ReceiverUnavailable("launch timed out")
                time.sleep(0.05)
        except NotConnected:
            raise
        except ReceiverUnavailable:
            raise
        except PyChromecastError as exc:
            reason = listener.launch_error
            raise ReceiverUnavailable(
                f"launch failed: {reason}" if reason else f"launch failed: {exc!r}",
                refused=isinstance(exc, RequestFailed) or reason == "CANCELLED",
            ) from exc
        finally:
            listener.launching = False

    async def send_receiver_message(self, payload: dict) -> None:
        tb = self._tellybox
        if tb is None or not tb.is_active:
            log.debug("receiver message not sent: the Tellybox receiver isn't running")
            return
        await self._run("send_receiver_message", lambda c: tb.send_message_nocheck(payload, no_add_request_id=True))

    async def stop_media(self) -> None:
        await self._run("stop_media", lambda c: c.media_controller.stop())

    async def pause(self) -> None:
        await self._run("pause", lambda c: c.media_controller.pause())

    async def resume(self) -> None:
        await self._run("resume", lambda c: c.media_controller.play())

    async def request_status(self) -> None:
        await self._run("request_status", lambda c: c.media_controller.update_status())

    async def quit_app(self) -> None:
        def run(c: Any) -> None:
            try:
                c.quit_app()
            except RequestFailed as exc:  # nothing to quit
                log.info("quit_app refused: %r", exc)

        await self._run("quit_app", run)

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
