import asyncio
import threading
from types import SimpleNamespace
from uuid import UUID

import pychromecast
import pytest
from pychromecast.error import NotConnected, RequestFailed, RequestTimeout

from tellybox.cast import pychromecast_device as mod
from tellybox.cast.device import (
    DEFAULT_MEDIA_RECEIVER as DMR,
    ConnectionState,
    ConnectionStatus,
    DeviceInfo,
    LoadFailed,
    MediaStatus,
    PlayerState,
    RECEIVER_NAMESPACE,
    ReceiverMessage,
    ReceiverStatus,
)
from tellybox.cast.pychromecast_device import (
    CastCommandError,
    PyChromecastDevice,
    ReceiverUnavailable,
    TellyboxController,
    discover,
)

UUID1 = "11111111-2222-3333-4444-555555555555"
INFO = DeviceInfo(uuid=UUID1, name="Living Room TV", host="192.168.1.137")


class StubMC:
    def __init__(self):
        self.calls = []
        self.listeners = []
        self.fail: dict[str, Exception] = {}

    def register_status_listener(self, listener):
        self.listeners.append(listener)

    def _call(self, name, *args, **kw):
        self.calls.append((name, args, kw))
        if name in self.fail:
            raise self.fail[name]

    is_active = True  # the running app implements the media namespace

    def play_media(self, *a, **kw): self._call("play_media", *a, **kw)
    def pause(self): self._call("pause")
    def play(self): self._call("play")
    def stop(self): self._call("stop")
    def update_status(self): self._call("update_status")


class StubCast:
    def __init__(self, host="192.168.1.137", wait_error=None):
        self.cast_info = SimpleNamespace(host=host, port=8009, friendly_name="Living Room TV", model_name="Chromecast")
        self.media_controller = StubMC()
        self.status_listeners, self.connection_listeners, self.launch_listeners = [], [], []
        self.wait_error = wait_error
        self.quit_error = None
        self.calls = []
        self.app_id = None
        self.app_namespaces: list[str] = []  # what the running app supports; the "socket client" is the stub itself
        self.handlers = []
        self.launch = "ok"  # ok | error | timeout | never_ready
        self.messages = []

    def wait(self, timeout=None):
        self.calls.append(("wait", timeout))
        if self.wait_error:
            raise self.wait_error

    def register_status_listener(self, l): self.status_listeners.append(l)
    def register_connection_listener(self, l): self.connection_listeners.append(l)
    def register_launch_error_listener(self, l): self.launch_listeners.append(l)

    def register_handler(self, handler):
        self.handlers.append(handler)
        handler.registered(self)

    def send_app_message(self, namespace, message, **kw):
        self.messages.append((namespace, message, kw))

    def start_app(self, app_id, force_launch=False, timeout=None):
        self.calls.append(("start_app", app_id, timeout))
        if self.launch == "error":
            for listener in self.launch_listeners:
                listener.new_launch_error(SimpleNamespace(reason="NOT_ALLOWED", app_id=app_id, request_id=1))
            raise RequestFailed("start app")
        if self.launch == "timeout":
            raise RequestTimeout("start app", timeout)
        if self.launch == "ok":
            self.app_id = app_id
            self.app_namespaces = ["urn:x-cast:com.google.cast.media", RECEIVER_NAMESPACE]

    def quit_app(self):
        self.calls.append(("quit_app",))
        if self.quit_error:
            raise self.quit_error

    def disconnect(self, timeout=None):
        self.calls.append(("disconnect",))


@pytest.fixture
def cast(monkeypatch):
    stub = StubCast()
    seen = []

    def from_host(host, **kw):
        seen.append(host)
        return stub

    monkeypatch.setattr(pychromecast, "get_chromecast_from_host", from_host)
    stub.seen = seen
    return stub


def in_thread(fn, *args):
    t = threading.Thread(target=fn, args=args)
    t.start()
    t.join()


async def next_events(dev, n):
    it = dev.events()
    return [await asyncio.wait_for(anext(it), 1) for _ in range(n)]


def cast_status(app_id, session_id, name=None):
    return SimpleNamespace(app_id=app_id, session_id=session_id, display_name=name)


def media_status(state, content_id="http://x/a.mp4", t=0.0, duration=600.0, idle_reason=None, msid=1):
    return SimpleNamespace(player_state=state, content_id=content_id, current_time=t, duration=duration,
                           idle_reason=idle_reason, media_session_id=msid)


async def test_connect_direct_to_remembered_host(cast):
    dev = PyChromecastDevice(INFO)
    await dev.connect(timeout=3.0)
    assert cast.seen == [("192.168.1.137", 8009, UUID(UUID1), None, "Living Room TV")]
    assert cast.calls[0] == ("wait", 3.0)
    assert len(cast.status_listeners) == len(cast.connection_listeners) == len(cast.media_controller.listeners) == 1


async def test_connect_falls_back_to_discovery_by_uuid(monkeypatch, cast):
    cast.wait_error = RequestTimeout("wait", 1)
    found = StubCast(host="192.168.1.50")
    browser = SimpleNamespace(stopped=False)
    browser.stop_discovery = lambda: setattr(browser, "stopped", True)
    args = {}

    def listed(**kw):
        args.update(kw)
        return [found], browser

    monkeypatch.setattr(pychromecast, "get_listed_chromecasts", listed)
    dev = PyChromecastDevice(INFO)
    await dev.connect(timeout=10.0)
    assert args["uuids"] == [UUID(UUID1)]
    assert ("disconnect",) in cast.calls
    assert dev.info.host == "192.168.1.50" and dev.info.model == "Chromecast"
    assert found.status_listeners and found.media_controller.listeners
    await dev.close()
    assert browser.stopped and ("disconnect",) in found.calls


async def test_connect_without_fallback_raises(cast):
    cast.wait_error = RequestTimeout("wait", 1)
    with pytest.raises(CastCommandError):
        await PyChromecastDevice(INFO, discovery_fallback=False).connect(timeout=1.0)
    assert ("disconnect",) in cast.calls


async def test_fallback_finds_nothing(monkeypatch, cast):
    cast.wait_error = RequestTimeout("wait", 1)
    browser = SimpleNamespace(stop_discovery=lambda: None)
    monkeypatch.setattr(pychromecast, "get_listed_chromecasts", lambda **kw: ([], browser))
    with pytest.raises(CastCommandError):
        await PyChromecastDevice(INFO).connect(timeout=1.0)


async def test_listener_callbacks_become_events_in_order(cast):
    dev = PyChromecastDevice(INFO)
    await dev.connect()
    conn, recv, med = cast.connection_listeners[0], cast.status_listeners[0], cast.media_controller.listeners[0]

    def fire():
        conn.new_connection_status(SimpleNamespace(status="CONNECTED", address=None, service=None))
        recv.new_cast_status(cast_status(DMR, "s1", "Default Media Receiver"))
        med.new_media_status(media_status("BUFFERING", t=None))
        med.new_media_status(media_status("PLAYING", t=12.5))
        med.new_media_status(media_status("WEIRD"))
        med.new_media_status(media_status("IDLE", t=0, idle_reason="FINISHED"))
        med.load_media_failed(3, 104)
        conn.new_connection_status(SimpleNamespace(status="FAILED_RESOLVE", address=None, service=None))
        conn.new_connection_status(SimpleNamespace(status="LOST", address=None, service=None))

    in_thread(fire)
    assert await next_events(dev, 9) == [
        ConnectionStatus(ConnectionState.CONNECTED),
        ReceiverStatus(DMR, "s1", "Default Media Receiver"),
        MediaStatus(PlayerState.BUFFERING, "http://x/a.mp4", 0.0, 600.0, None, 1),
        MediaStatus(PlayerState.PLAYING, "http://x/a.mp4", 12.5, 600.0, None, 1),
        MediaStatus(PlayerState.UNKNOWN, "http://x/a.mp4", 0.0, 600.0, None, 1),
        MediaStatus(PlayerState.IDLE, "http://x/a.mp4", 0.0, 600.0, "FINISHED", 1),
        LoadFailed(104),
        ConnectionStatus(ConnectionState.FAILED),
        ConnectionStatus(ConnectionState.LOST),
    ]
    assert dev.receiver == ReceiverStatus(DMR, "s1", "Default Media Receiver")
    assert dev.media.player_state == PlayerState.IDLE


async def test_launch_error_becomes_load_failed(cast):
    dev = PyChromecastDevice(INFO)
    await dev.connect()
    in_thread(cast.launch_listeners[0].new_launch_error, SimpleNamespace(reason="X", app_id=DMR, request_id=1))
    assert await next_events(dev, 1) == [LoadFailed(None)]


async def test_play_uses_buffered_mp4(cast):
    dev = PyChromecastDevice(INFO)
    await dev.connect()
    await dev.play("http://x/a.mp4", title="Ep", start_s=42.0)
    await dev.play("http://x/b.mp4")
    mc = cast.media_controller
    assert mc.calls == [
        ("play_media", ("http://x/a.mp4", "video/mp4"), {"title": "Ep", "stream_type": "BUFFERED", "current_time": 42.0}),
        ("play_media", ("http://x/b.mp4", "video/mp4"), {"title": None, "stream_type": "BUFFERED", "current_time": None}),
    ]


async def test_play_without_app_id_leaves_another_media_app(cast):  # PB-2, WT-9
    """Our media only ever loads into the Default Media Receiver without an app id, even when another app
    that speaks the media namespace (the Tellybox receiver after its app id was cleared) is running."""
    dev = PyChromecastDevice(INFO)
    await dev.connect()
    cast.app_id = "ABCD1234"
    await dev.play("http://x/a.mp4")
    assert ("start_app", mod.DEFAULT_MEDIA_RECEIVER, mod.RECEIVER_LAUNCH_TIMEOUT_S) in cast.calls
    cast.calls.clear()
    await dev.play("http://x/b.mp4")  # the Default Media Receiver runs now: no relaunch
    assert not [c for c in cast.calls if c[0] == "start_app"]


async def test_simple_commands(cast):
    dev = PyChromecastDevice(INFO)
    await dev.connect()
    await dev.pause()
    await dev.resume()
    await dev.request_status()
    assert [c[0] for c in cast.media_controller.calls] == ["pause", "play", "update_status"]


async def test_stop_stops_media_then_quits_app(cast):
    dev = PyChromecastDevice(INFO)
    await dev.connect()
    await dev.stop()
    assert [c[0] for c in cast.media_controller.calls] == ["stop"]
    assert cast.calls[-1] == ("quit_app",)


async def test_stop_tolerates_no_media_session(cast):
    dev = PyChromecastDevice(INFO)
    await dev.connect()
    cast.media_controller.fail["stop"] = RequestFailed("stop")
    cast.quit_error = RequestFailed("quit app")
    await dev.stop()
    assert cast.calls[-1] == ("quit_app",)


async def test_stop_raises_when_quit_times_out(cast):
    dev = PyChromecastDevice(INFO)
    await dev.connect()
    cast.quit_error = RequestTimeout("quit app", 10)
    with pytest.raises(CastCommandError):
        await dev.stop()


@pytest.mark.parametrize("exc", [NotConnected(), RequestTimeout("pause", 10), RequestFailed("pause")])
async def test_errors_become_cast_command_error(cast, exc):
    dev = PyChromecastDevice(INFO)
    await dev.connect()
    cast.media_controller.fail["pause"] = exc
    with pytest.raises(CastCommandError) as err:
        await dev.pause()
    assert err.value.__cause__ is exc


async def test_commands_before_connect_raise():
    with pytest.raises(CastCommandError):
        await PyChromecastDevice(INFO).pause()


async def test_close_disconnects_and_ignores_late_callbacks(cast):
    dev = PyChromecastDevice(INFO)
    await dev.connect()
    await dev.close()
    assert ("disconnect",) in cast.calls
    in_thread(cast.status_listeners[0].new_cast_status, cast_status(None, None))
    await asyncio.sleep(0.01)
    assert dev.receiver is None


async def test_discover(monkeypatch):
    infos = [SimpleNamespace(uuid=UUID(UUID1), friendly_name="Living Room TV", host="192.168.1.137",
                             port=8009, model_name="Chromecast")]
    browser = SimpleNamespace(stopped=False)
    browser.stop_discovery = lambda: setattr(browser, "stopped", True)
    got = {}

    def fake_discover(**kw):
        got.update(kw)
        return infos, browser

    monkeypatch.setattr(pychromecast.discovery, "discover_chromecasts", fake_discover)
    assert await discover(timeout=1.0, known_hosts=["192.168.1.137"]) == [
        DeviceInfo(UUID1, "Living Room TV", "192.168.1.137", 8009, "Chromecast")
    ]
    assert got == {"timeout": 1.0, "known_hosts": ["192.168.1.137"]} and browser.stopped


# --------------------------------------------------------------------------- Tellybox receiver (v7)

TB = "ABCD1234"


async def test_play_with_app_id_launches_our_app_then_loads(cast):  # CR-1
    dev = PyChromecastDevice(INFO)
    await dev.connect()
    await dev.play("http://x/a.mp4", title="Ep", start_s=5.0, app_id=TB)
    assert cast.calls[-1] == ("start_app", TB, mod.RECEIVER_LAUNCH_TIMEOUT_S)
    assert [c[0] for c in cast.media_controller.calls] == ["play_media"]


async def test_running_app_is_not_relaunched(cast):
    dev = PyChromecastDevice(INFO)
    await dev.connect()
    cast.app_id = TB
    await dev.play("http://x/a.mp4", app_id=TB)
    assert not [c for c in cast.calls if c[0] == "start_app"]
    assert [c[0] for c in cast.media_controller.calls] == ["play_media"]


async def test_launch_error_raises_receiver_unavailable_and_is_not_a_load_failure(cast):  # CR-6
    dev = PyChromecastDevice(INFO)
    await dev.connect()
    cast.launch = "error"
    with pytest.raises(ReceiverUnavailable, match="NOT_ALLOWED"):
        await dev.play("http://x/a.mp4", app_id=TB)
    assert not cast.media_controller.calls
    await asyncio.sleep(0.01)
    assert dev._queue.empty()  # a failed receiver launch must not end the episode as LoadFailed


async def test_launch_request_timeout_raises_receiver_unavailable(cast):  # CR-6
    dev = PyChromecastDevice(INFO)
    await dev.connect()
    cast.launch = "timeout"
    with pytest.raises(ReceiverUnavailable):
        await dev.play("http://x/a.mp4", app_id=TB)
    assert not cast.media_controller.calls


async def test_app_that_never_shows_up_times_out(cast):  # CR-6, S8
    dev = PyChromecastDevice(INFO)
    await dev.connect()
    cast.launch = "never_ready"
    with pytest.raises(ReceiverUnavailable, match="timed out") as err:
        await dev.play("http://x/a.mp4", app_id=TB, launch_timeout_s=0.2)
    assert err.value.refused is False  # a timeout is worth another try
    assert cast.calls[-1] == ("start_app", TB, 0.2)
    assert not cast.media_controller.calls


async def test_a_rejected_app_is_a_refusal(cast):  # CR-6, S8
    dev = PyChromecastDevice(INFO)
    await dev.connect()
    cast.launch = "error"  # RequestFailed at once, as for an unregistered app id
    with pytest.raises(ReceiverUnavailable) as err:
        await dev.play("http://x/a.mp4", app_id=TB)
    assert err.value.refused is True


async def test_a_cancelled_launch_is_a_refusal(cast):  # CR-6
    dev = PyChromecastDevice(INFO)
    await dev.connect()
    cast.launch = "never_ready"
    threading.Timer(0.1, lambda: cast.launch_listeners[0].new_launch_error(
        SimpleNamespace(reason="CANCELLED", app_id=TB, request_id=1))).start()
    with pytest.raises(ReceiverUnavailable, match="CANCELLED") as err:
        await dev.play("http://x/a.mp4", app_id=TB, launch_timeout_s=2.0)
    assert err.value.refused is True


async def test_a_request_timeout_is_not_a_refusal(cast):  # CR-6
    dev = PyChromecastDevice(INFO)
    await dev.connect()
    cast.launch = "timeout"
    with pytest.raises(ReceiverUnavailable) as err:
        await dev.play("http://x/a.mp4", app_id=TB)
    assert err.value.refused is False


async def test_quit_app_tolerates_nothing_to_quit(cast):  # CR-6
    dev = PyChromecastDevice(INFO)
    await dev.connect()
    await dev.quit_app()
    cast.quit_error = RequestFailed("quit app")
    await dev.quit_app()
    assert [c for c in cast.calls if c[0] == "quit_app"] == [("quit_app",)] * 2


async def test_waits_for_the_media_namespace_before_loading(cast):
    dev = PyChromecastDevice(INFO)
    await dev.connect()
    cast.media_controller.is_active = False  # our app is up but has not registered the media namespace yet
    threading.Timer(0.15, lambda: setattr(cast.media_controller, "is_active", True)).start()
    await dev.play("http://x/a.mp4", app_id=TB)
    assert [c[0] for c in cast.media_controller.calls] == ["play_media"]


async def test_launch_when_not_connected_is_a_plain_command_error():
    with pytest.raises(CastCommandError) as err:
        await PyChromecastDevice(INFO).play("http://x/a.mp4", app_id=TB)
    assert not isinstance(err.value, ReceiverUnavailable)


async def test_custom_namespace_messages_become_events(cast):  # CR-7
    dev = PyChromecastDevice(INFO)
    await dev.connect()
    (handler,) = cast.handlers
    assert isinstance(handler, TellyboxController) and handler.namespace == RECEIVER_NAMESPACE
    in_thread(handler.receive_message, None, {"type": "hello", "v": 1, "ua": "X"})
    assert await next_events(dev, 1) == [ReceiverMessage({"type": "hello", "v": 1, "ua": "X"})]


async def test_send_receiver_message_needs_our_app_running(cast):
    dev = PyChromecastDevice(INFO)
    await dev.connect()
    await dev.send_receiver_message({"type": "state"})
    assert cast.messages == []  # a foreign app runs: nothing is sent
    cast.app_namespaces = [RECEIVER_NAMESPACE]
    await dev.send_receiver_message({"type": "state", "v": 1})
    ((ns, message, kw),) = cast.messages
    assert (ns, message) == (RECEIVER_NAMESPACE, {"type": "state", "v": 1}) and kw["no_add_request_id"] is True


async def test_stop_media_keeps_the_app(cast):
    dev = PyChromecastDevice(INFO)
    await dev.connect()
    await dev.stop_media()
    assert [c[0] for c in cast.media_controller.calls] == ["stop"]
    assert ("quit_app",) not in cast.calls


def test_module_exports():
    assert issubclass(mod.CastCommandError, Exception)
