"""The Chromecast as seen by the controller.

`CastDevice` is the only boundary to the real device. Everything above it
(controller, timer) is tested with `tellybox.cast.fake.FakeCastDevice`.

Spike findings that shaped this interface (docs/spike-casting.md):
- The device only pushes status on state changes; there is no periodic update
  while playing, so callers must track time themselves.
- Media status from *other* apps (e.g. YouTube) also arrives here; callers must
  filter on receiver session id and content id (WT-9).
- `current_time` is 0 in the IDLE/FINISHED status.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

DEFAULT_MEDIA_RECEIVER = "CC1AD845"

# The Tellybox receiver (v7, CR-1..CR-8). Its app id comes from settings.receiver_app_id; the
# messages on this namespace are in docs/receiver-protocol.md.
RECEIVER_NAMESPACE = "urn:x-cast:tellybox"
RECEIVER_LAUNCH_TIMEOUT_S = 8.0  # launch + first status; a cold launch takes ~3 s on the 1st gen (spike S1)
RECEIVER_RETRY_LAUNCH_TIMEOUT_S = 15.0  # CR-6: the second attempt waits longer; a slow device may just need time


class PlayerState(StrEnum):
    PLAYING = "PLAYING"
    BUFFERING = "BUFFERING"
    PAUSED = "PAUSED"
    IDLE = "IDLE"
    UNKNOWN = "UNKNOWN"


class ConnectionState(StrEnum):
    CONNECTING = "CONNECTING"
    CONNECTED = "CONNECTED"
    LOST = "LOST"
    FAILED = "FAILED"
    DISCONNECTED = "DISCONNECTED"


@dataclass(frozen=True)
class DeviceInfo:
    uuid: str
    name: str | None
    host: str
    port: int = 8009
    model: str | None = None


@dataclass(frozen=True)
class ReceiverStatus:
    """Which app runs on the device. app_id/session_id are None when idle/backdrop-less."""

    app_id: str | None
    session_id: str | None
    display_name: str | None = None


@dataclass(frozen=True)
class MediaStatus:
    player_state: PlayerState
    content_id: str | None
    current_time: float
    duration: float | None
    idle_reason: str | None = None  # FINISHED | CANCELLED | INTERRUPTED | ERROR
    media_session_id: int | None = None


@dataclass(frozen=True)
class ConnectionStatus:
    state: ConnectionState


@dataclass(frozen=True)
class LoadFailed:
    error_code: int | None = None


@dataclass(frozen=True)
class ReceiverMessage:
    """A JSON message from the Tellybox receiver on RECEIVER_NAMESPACE (CR-7)."""

    payload: dict


DeviceEvent = ReceiverStatus | MediaStatus | ConnectionStatus | LoadFailed | ReceiverMessage


class CastDevice(Protocol):
    """One Chromecast. All methods are async and must not block the event loop."""

    info: DeviceInfo | None

    async def connect(self, timeout: float = 15.0) -> None:
        """Connect (and keep reconnecting in the background after losses)."""
        ...

    async def close(self) -> None: ...

    def events(self) -> AsyncIterator[DeviceEvent]:
        """Every status change, in order. Single consumer."""
        ...

    @property
    def receiver(self) -> ReceiverStatus | None:
        """Last known receiver status."""
        ...

    @property
    def media(self) -> MediaStatus | None:
        """Last known media status (may belong to another app)."""
        ...

    async def play(
        self,
        url: str,
        *,
        title: str | None = None,
        start_s: float = 0.0,
        app_id: str | None = None,
        launch_timeout_s: float = RECEIVER_LAUNCH_TIMEOUT_S,
    ) -> None:
        """Load `url` as BUFFERED video/mp4. With `app_id` (the Tellybox receiver), launch that app first
        if it isn't running and raise ReceiverUnavailable if it can't launch within `launch_timeout_s`
        (CR-6); `refused` is set when the device rejects the app outright. Without, launch the Default
        Media Receiver if needed."""
        ...

    async def quit_app(self) -> None:
        """Quit the running receiver app (best effort; nothing to quit is not an error). Used to clear a
        half-started Tellybox receiver before a second launch attempt (CR-6)."""
        ...

    async def send_receiver_message(self, payload: dict) -> None:
        """Send `payload` as JSON on RECEIVER_NAMESPACE. Fire and forget; does nothing unless the
        Tellybox receiver is the running app (CR-7)."""
        ...

    async def pause(self) -> None: ...

    async def resume(self) -> None: ...

    async def stop(self) -> None:
        """Stop media and quit the receiver app, so the TV returns to its idle screen."""
        ...

    async def stop_media(self) -> None:
        """Stop the media but keep the receiver app running (the night screen after time's up, CR-3)."""
        ...

    async def request_status(self) -> None:
        """Ask the device for a fresh media status (result arrives as an event)."""
        ...
