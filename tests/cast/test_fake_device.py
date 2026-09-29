import asyncio

import pytest

from tellybox.cast.device import (
    DEFAULT_MEDIA_RECEIVER as DMR,
    ConnectionState,
    ConnectionStatus,
    MediaStatus,
    PlayerState,
    ReceiverMessage,
    ReceiverStatus,
)
from tellybox.cast.fake import BACKDROP_APP, YOUTUBE_APP, FakeCastDevice
from tellybox.cast.pychromecast_device import CastCommandError, ReceiverUnavailable
from tellybox.clock import FakeClock

URL = "http://srv/media/ep1.mp4"
P, B, PA, I = PlayerState.PLAYING, PlayerState.BUFFERING, PlayerState.PAUSED, PlayerState.IDLE


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def dev(clock):
    return FakeCastDevice(clock)


async def connected(dev):
    await dev.connect()
    dev.drain()
    return dev


async def test_defaults(dev):
    assert dev.info.uuid == "00000000-0000-0000-0000-000000000001"
    assert dev.info.name == "Fake TV" and dev.info.host == "127.0.0.1"
    assert not dev.connected and dev.receiver is None and dev.media is None


async def test_connect_emits_connection_and_backdrop(dev):
    await dev.connect()
    assert dev.connected
    assert dev.drain() == [
        ConnectionStatus(ConnectionState.CONNECTED),
        ReceiverStatus(BACKDROP_APP, "backdrop-1", "Backdrop"),
    ]
    assert dev.calls == [("connect",)]
    assert dev.receiver == ReceiverStatus(BACKDROP_APP, "backdrop-1", "Backdrop")


async def test_play_cold_launches_receiver_and_loads(dev):
    dev.durations["ep1"] = 1200.0
    await connected(dev)
    await dev.play(URL, title="Ep 1", start_s=30.0)
    assert dev.drain() == [
        ReceiverStatus(DMR, "dmr-1", "Default Media Receiver"),
        MediaStatus(I, URL, 0.0, None, media_session_id=1),
        MediaStatus(B, URL, 30.0, 1200.0, media_session_id=1),
        MediaStatus(P, URL, 30.0, 1200.0, media_session_id=1),
    ]
    assert dev.calls[-1] == ("play", URL, 30.0)
    assert dev.media == MediaStatus(P, URL, 30.0, 1200.0, media_session_id=1)


async def test_play_warm_reuses_receiver(dev):
    await connected(dev)
    await dev.play(URL)
    dev.drain()
    await dev.play("http://srv/media/ep2.mp4")
    assert dev.drain() == [
        MediaStatus(I, "http://srv/media/ep2.mp4", 0.0, None, media_session_id=2),
        # The replaced media, reported with the new URL (pychromecast keeps the last contentId).
        MediaStatus(I, "http://srv/media/ep2.mp4", 0.0, None, "INTERRUPTED", media_session_id=1),
        MediaStatus(B, "http://srv/media/ep2.mp4", 0.0, 600.0, media_session_id=2),
        MediaStatus(P, "http://srv/media/ep2.mp4", 0.0, 600.0, media_session_id=2),
    ]


async def test_position_advances_only_while_playing(dev, clock):
    await connected(dev)
    await dev.play(URL, start_s=10.0)
    clock.advance(5)
    assert dev.position() == 15.0
    await dev.pause()
    assert dev.drain()[-1] == MediaStatus(PA, URL, 15.0, 600.0, media_session_id=1)
    clock.advance(100)
    assert dev.position() == 15.0
    await dev.resume()
    assert dev.drain() == [MediaStatus(P, URL, 15.0, 600.0, media_session_id=1)]
    clock.advance(3)
    dev.buffer()
    assert dev.drain() == [MediaStatus(B, URL, 18.0, 600.0, media_session_id=1)]
    clock.advance(50)
    assert dev.position() == 18.0
    dev.external_resume()
    clock.advance(2)
    assert dev.position() == 20.0
    clock.advance(10_000)
    assert dev.position() == 600.0  # capped at duration


async def test_external_pause_and_resume(dev, clock):
    await connected(dev)
    await dev.play(URL)
    dev.drain()
    clock.advance(7)
    dev.external_pause()
    dev.external_resume()
    assert dev.drain() == [
        MediaStatus(PA, URL, 7.0, 600.0, media_session_id=1),
        MediaStatus(P, URL, 7.0, 600.0, media_session_id=1),
    ]
    assert ("pause",) not in dev.calls


async def test_stop_cancels_and_returns_to_backdrop(dev, clock):
    await connected(dev)
    await dev.play(URL)
    dev.drain()
    clock.advance(20)
    await dev.stop()
    assert dev.drain() == [
        MediaStatus(I, URL, 0.0, 600.0, idle_reason="CANCELLED", media_session_id=1),
        ReceiverStatus(BACKDROP_APP, "backdrop-2", "Backdrop"),
    ]
    assert dev.calls[-1] == ("stop",)


async def test_finish_reports_zero_position_and_keeps_receiver(dev, clock):
    await connected(dev)
    await dev.play(URL)
    dev.drain()
    clock.advance(600)
    dev.finish()
    assert dev.drain() == [MediaStatus(I, URL, 0.0, 600.0, idle_reason="FINISHED", media_session_id=1)]
    assert dev.receiver.app_id == DMR
    assert dev.position() == 0.0


async def test_request_status_reemits_with_current_position(dev, clock):
    await connected(dev)
    await dev.play(URL)
    dev.drain()
    clock.advance(42)
    await dev.request_status()
    assert dev.drain() == [MediaStatus(P, URL, 42.0, 600.0, media_session_id=1)]
    assert dev.calls[-1] == ("request_status",)


async def test_takeover_and_end_foreign(dev):
    await connected(dev)
    await dev.play(URL)
    dev.drain()
    dev.takeover()
    assert dev.drain() == [
        ReceiverStatus(None, None),
        ReceiverStatus(YOUTUBE_APP, "foreign-1", "YouTube"),
        MediaStatus(P, "yt-abc123", 0.0, 300.0),
    ]
    dev.end_foreign()
    assert dev.drain() == [ReceiverStatus(BACKDROP_APP, "backdrop-2", "Backdrop")]


async def test_lose_connection_and_reconnect(dev, clock):
    await connected(dev)
    await dev.play(URL)
    dev.drain()
    clock.advance(10)
    dev.lose_connection()
    assert not dev.connected
    assert dev.drain() == [ConnectionStatus(ConnectionState.LOST)]
    clock.advance(30)
    assert dev.position() == 10.0  # frozen while disconnected
    with pytest.raises(CastCommandError):
        await dev.pause()
    dev.reconnect()
    assert dev.drain() == [
        ConnectionStatus(ConnectionState.CONNECTED),
        ReceiverStatus(DMR, "dmr-1", "Default Media Receiver"),
        MediaStatus(P, URL, 10.0, 600.0, media_session_id=1),
    ]
    clock.advance(1)
    assert dev.position() == 11.0


async def test_helpers_while_disconnected_emit_nothing(dev):
    await connected(dev)
    await dev.play(URL)
    dev.lose_connection()
    dev.drain()
    dev.finish()
    assert dev.drain() == []
    dev.reconnect()
    assert dev.drain()[-1] == MediaStatus(I, URL, 0.0, 600.0, idle_reason="FINISHED", media_session_id=1)


async def test_power_cycle(dev):
    await connected(dev)
    await dev.play(URL)
    dev.drain()
    dev.power_cycle()
    assert dev.drain() == [ConnectionStatus(ConnectionState.LOST)]
    dev.reconnect()
    assert dev.drain() == [
        ConnectionStatus(ConnectionState.CONNECTED),
        ReceiverStatus(BACKDROP_APP, "backdrop-2", "Backdrop"),
    ]
    assert dev.position() == 0.0


async def test_preload_then_connect(dev, clock):
    dev.preload(URL, 123.0, duration_s=900.0)
    assert dev.drain() == []
    await dev.connect()
    assert dev.drain() == [
        ConnectionStatus(ConnectionState.CONNECTED),
        ReceiverStatus(DMR, "dmr-1", "Default Media Receiver"),
        MediaStatus(P, URL, 123.0, 900.0, media_session_id=1),
    ]
    clock.advance(2)
    assert dev.position() == 125.0


async def test_preload_paused(dev, clock):
    dev.preload(URL, 50.0, state=PA)
    await dev.connect()
    assert dev.drain()[-1] == MediaStatus(PA, URL, 50.0, 600.0, media_session_id=1)
    clock.advance(20)
    assert dev.position() == 50.0


@pytest.mark.parametrize("cmd", ["play", "pause", "resume", "stop", "request_status"])
async def test_commands_fail_when_not_connected(dev, cmd):
    args = (URL,) if cmd == "play" else ()
    with pytest.raises(CastCommandError):
        await getattr(dev, cmd)(*args)
    assert dev.drain() == []


async def test_fail_next_command(dev):
    await connected(dev)
    dev.fail_next_command = True
    with pytest.raises(CastCommandError):
        await dev.play(URL)
    assert dev.drain() == [] and not dev.fail_next_command
    await dev.play(URL)
    assert dev.drain()[-1].player_state == P


async def test_events_iterator(dev):
    await dev.connect()
    it = dev.events()
    assert await asyncio.wait_for(anext(it), 1) == ConnectionStatus(ConnectionState.CONNECTED)
    assert (await asyncio.wait_for(anext(it), 1)).app_id == BACKDROP_APP


async def test_close(dev):
    await connected(dev)
    await dev.close()
    assert not dev.connected and dev.calls[-1] == ("close",)


# --------------------------------------------------------------------------- Tellybox receiver (v7)

TB = "ABCD1234"


async def test_play_with_app_id_launches_our_receiver_and_says_hello(dev):  # CR-1
    await connected(dev)
    await dev.play(URL, app_id=TB)
    events = dev.drain()
    assert ReceiverStatus(TB, "tellybox-1", "Tellybox") in events
    hello = [e for e in events if isinstance(e, ReceiverMessage)]
    assert len(hello) == 1 and hello[0].payload["type"] == "hello" and hello[0].payload["v"] == 1
    assert events.index(hello[0]) < events.index(next(e for e in events if isinstance(e, MediaStatus)))
    assert dev.receiver.app_id == TB and dev.media.player_state == P
    assert dev.play_app_ids == [TB]


async def test_warm_receiver_is_not_relaunched(dev):
    await connected(dev)
    await dev.play(URL, app_id=TB)
    dev.drain()
    await dev.play("http://srv/media/ep2.mp4", app_id=TB)
    events = dev.drain()
    assert not [e for e in events if isinstance(e, (ReceiverStatus, ReceiverMessage))]
    assert dev.receiver.session_id == "tellybox-1"


async def test_launch_failure_injection(dev):  # CR-6
    await connected(dev)
    dev.fail_launch = True
    with pytest.raises(ReceiverUnavailable):
        await dev.play(URL, app_id=TB)
    assert dev.drain() == [] and dev.receiver.app_id == BACKDROP_APP
    dev.fail_launch = False
    await dev.play(URL)  # the Default Media Receiver is unaffected
    assert dev.receiver.app_id == DMR


async def test_messages_are_recorded_only_while_our_receiver_runs(dev):
    await connected(dev)
    await dev.send_receiver_message({"type": "state"})
    assert dev.sent_messages == []
    await dev.play(URL, app_id=TB)
    await dev.send_receiver_message({"type": "state", "n": 1})
    assert dev.sent_messages == [{"type": "state", "n": 1}]
    await dev.stop()
    await dev.send_receiver_message({"type": "state", "n": 2})
    assert len(dev.sent_messages) == 1


async def test_stop_media_keeps_the_app(dev):
    await connected(dev)
    await dev.play(URL, app_id=TB)
    dev.drain()
    await dev.stop_media()
    assert dev.receiver.app_id == TB
    assert dev.drain() == [MediaStatus(I, URL, 0.0, 600.0, "CANCELLED", 1)]


async def test_receiver_says_hello_on_demand(dev):
    await connected(dev)
    await dev.play(URL, app_id=TB)
    dev.drain()
    dev.hello()
    assert [e.payload["type"] for e in dev.drain()] == ["hello"]
