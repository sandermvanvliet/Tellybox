"""Typed events of the cast service (HA-13), with a fake clock and a fake Chromecast.

Every place a session starts or ends has a test: TV start (_start_episode), TV end (_end_current, reached from
replace, finish, stop, parent stop, time up, block, take-over, disconnect, load failure), in-app start
(_start_device_session) and in-app end (_end_device). Overrides, the time_up / last_five edges, the bus and the
SSE endpoint follow.
"""

import asyncio
import json
import logging
from datetime import timedelta

import pytest

from tellybox.cast import api as cast_api
from tellybox.cast import events as typed
from tellybox.cast.controller import CastController
from tellybox.cast.events import EventBus
from tellybox.cast.pychromecast_device import CastCommandError
from tellybox.db import to_db

from test_api import env  # noqa: F401  (fixture)
from test_controller import (  # noqa: F401  (fixtures)
    EPISODE_S,
    START,
    TZ,
    clock,
    configure,
    conn,
    episodes,
    fake,
    make_controller,
    play,
    pump,
    run_for,
)
from test_controller_devices import DEV, DEV2, beat, kids, watch  # noqa: F401  (fixtures)


def drain(q: asyncio.Queue) -> list[dict]:
    out = []
    while not q.empty():
        out.append(q.get_nowait())
    return out


async def listening(conn, clock, fake):
    """A controller with an event subscription; returns (controller, queue)."""
    ctrl = await make_controller(conn, clock, fake)
    return ctrl, ctrl.subscribe_events()


def kinds(events: list[dict]) -> list[str]:
    return [e["type"] for e in events]


# --------------------------------------------------------------------------- the TV: start


async def test_playback_started_has_every_field(conn, clock, fake, episodes):  # HA-13
    ctrl, q = await listening(conn, clock, fake)
    await play(ctrl, fake, episodes[0])
    assert drain(q) == [{
        "type": "playback_started", "at": to_db(START), "profile_ids": [1], "episode_id": episodes[0], "show_id": 1,
        "title": "Episode 1", "show": "Dev show", "target": "tv", "label": fake.info.name,
    }]


async def test_a_group_pick_lists_every_profile_sorted(conn, clock, fake, episodes, kids):  # PR-2
    ctrl, q = await listening(conn, clock, fake)
    await ctrl.play(episodes[0], [3, 1])
    assert drain(q)[0]["profile_ids"] == [1, 3]


async def test_a_refused_pick_emits_nothing(conn, clock, fake, episodes):  # WT-7
    ctrl, q = await listening(conn, clock, fake)
    await ctrl.override("block")
    drain(q)
    with pytest.raises(Exception):  # noqa: B017
        await ctrl.play(episodes[0])
    assert drain(q) == []


# --------------------------------------------------------------------------- the TV: every end


async def stopped(q) -> dict:
    (event,) = [e for e in drain(q) if e["type"] == "playback_stopped"]
    return event


async def test_replaced_gives_stopped_then_started(conn, clock, fake, episodes):  # A-5
    ctrl, q = await listening(conn, clock, fake)
    await play(ctrl, fake, episodes[0])
    await run_for(ctrl, fake, clock, 30)
    drain(q)
    await play(ctrl, fake, episodes[1])
    events = drain(q)
    assert kinds(events) == ["playback_stopped", "playback_started"]
    assert events[0]["reason"] == "replaced" and events[0]["episode_id"] == episodes[0]
    assert events[0]["position_s"] == pytest.approx(30, abs=1)
    assert events[1]["episode_id"] == episodes[1]


async def test_autoplay_gives_stopped_finished_then_started(conn, clock, fake, episodes):  # PB-3
    ctrl, q = await listening(conn, clock, fake)
    await play(ctrl, fake, episodes[0])
    drain(q)
    await run_for(ctrl, fake, clock, EPISODE_S + 2)
    events = [e for e in drain(q) if e["type"].startswith("playback_")]
    assert kinds(events) == ["playback_stopped", "playback_started"]
    assert events[0]["reason"] == "finished" and events[0]["episode_id"] == episodes[0]
    assert events[0]["position_s"] == pytest.approx(EPISODE_S, abs=1)
    assert events[1]["episode_id"] == episodes[1]


async def test_finished_without_autoplay_is_a_lone_stopped(conn, clock, fake, episodes):  # LM-4
    conn.execute("UPDATE show SET autoplay = 0")
    ctrl, q = await listening(conn, clock, fake)
    await play(ctrl, fake, episodes[0])
    drain(q)
    await run_for(ctrl, fake, clock, EPISODE_S + 2)
    events = drain(q)
    assert kinds(events) == ["playback_stopped"] and events[0]["reason"] == "finished"


async def test_stop_from_the_api_is_stopped(conn, clock, fake, episodes):
    ctrl, q = await listening(conn, clock, fake)
    await play(ctrl, fake, episodes[0])
    drain(q)
    await ctrl.stop()
    assert (await stopped(q))["reason"] == "stopped"


async def test_stop_from_another_controller_is_stopped(conn, clock, fake, episodes):  # PB-5
    from tellybox.cast.device import MediaStatus, PlayerState

    ctrl, q = await listening(conn, clock, fake)
    await play(ctrl, fake, episodes[0])
    drain(q)
    await ctrl.handle_event(MediaStatus(PlayerState.IDLE, ctrl.current.url, 0.0, EPISODE_S, idle_reason="CANCELLED"))
    assert (await stopped(q))["reason"] == "stopped"


async def test_parent_stop_now(conn, clock, fake, episodes):  # WT-7
    ctrl, q = await listening(conn, clock, fake)
    await play(ctrl, fake, episodes[0])
    drain(q)
    await ctrl.override("stop_now", source="Home Assistant")
    events = drain(q)
    assert kinds(events) == ["playback_stopped", "override_applied"]
    assert events[0]["reason"] == "parent_stop"


async def test_time_up_stops_with_reason_time_up(conn, clock, fake, episodes):  # WT-4, WT-5
    configure(conn, allowance_min=7, grace_min=0)
    ctrl, q = await listening(conn, clock, fake)
    await play(ctrl, fake, episodes[0])
    drain(q)
    await run_for(ctrl, fake, clock, 425)
    events = drain(q)
    assert kinds(events) == ["last_five", "time_up", "playback_stopped"]
    assert events[2]["reason"] == "time_up" and events[2]["position_s"] == pytest.approx(420, abs=2)


async def test_block_stops_with_reason_blocked(conn, clock, fake, episodes):  # WT-7
    ctrl, q = await listening(conn, clock, fake)
    await play(ctrl, fake, episodes[0])
    drain(q)
    await ctrl.override("block")
    events = drain(q)
    assert kinds(events) == ["override_applied", "time_up", "playback_stopped"]
    assert events[1]["reason"] == "blocked" and events[2]["reason"] == "blocked"


async def test_taken_over(conn, clock, fake, episodes):  # WT-9
    ctrl, q = await listening(conn, clock, fake)
    await play(ctrl, fake, episodes[0])
    drain(q)
    fake.takeover()
    await pump(ctrl, fake)
    assert (await stopped(q))["reason"] == "taken_over"


async def test_disconnected_after_a_long_loss(conn, clock, fake, episodes):  # PB-5
    ctrl, q = await listening(conn, clock, fake)
    await play(ctrl, fake, episodes[0])
    await run_for(ctrl, fake, clock, 30)
    drain(q)
    fake.lose_connection()
    await pump(ctrl, fake)
    await run_for(ctrl, fake, clock, 301)
    event = await stopped(q)
    assert event["reason"] == "disconnected" and event["position_s"] == pytest.approx(30, abs=2)


async def test_a_failed_load_gives_started_then_stopped_load_failed(conn, clock, fake, episodes):  # PB-9
    ctrl, q = await listening(conn, clock, fake)
    fake.fail_next_command = True
    with pytest.raises(CastCommandError):
        await ctrl.play(episodes[0])
    events = drain(q)
    assert kinds(events) == ["playback_started", "playback_stopped"]
    assert events[1]["reason"] == "load_failed" and events[1]["position_s"] == 0


async def test_the_device_switch_ends_the_session_as_stopped(conn, clock, fake, episodes):  # PB-1
    ctrl, q = await listening(conn, clock, fake)
    await play(ctrl, fake, episodes[0])
    drain(q)
    from tellybox.cast.fake import FakeCastDevice

    await ctrl.set_device(FakeCastDevice(clock))
    assert (await stopped(q))["reason"] == "stopped"
    await ctrl.stop_service()


# --------------------------------------------------------------------------- the kid app (device sessions)


async def test_device_session_start_and_stop(conn, clock, fake, episodes):  # PB-7
    ctrl, q = await listening(conn, clock, fake)
    await ctrl.device_play(DEV, "iPhone Safari", episodes[0], [1])
    (started,) = drain(q)
    assert started == {
        "type": "playback_started", "at": to_db(clock.now()), "profile_ids": [1], "episode_id": episodes[0],
        "show_id": 1, "title": "Episode 1", "show": "Dev show", "target": "device", "label": "iPhone Safari",
    }
    await watch(ctrl, clock, DEV, 30, 0)
    await ctrl.device_stop(DEV)
    (event,) = drain(q)
    assert event["type"] == "playback_stopped" and event["reason"] == "stopped" and event["target"] == "device"
    assert event["label"] == "iPhone Safari" and event["position_s"] == pytest.approx(30, abs=1)


async def test_device_replace_error_and_disconnect(conn, clock, fake, episodes):  # PB-8, PB-9, WT-10
    ctrl, q = await listening(conn, clock, fake)
    await ctrl.device_play(DEV, "A phone", episodes[0], [1])
    await ctrl.device_play(DEV, "A phone", episodes[1], [1])
    events = drain(q)
    assert kinds(events) == ["playback_started", "playback_stopped", "playback_started"]
    assert events[1]["reason"] == "replaced"
    await ctrl.device_heartbeat(DEV, "error", 0)
    assert drain(q)[0]["reason"] == "load_failed"
    await ctrl.device_play(DEV, "A phone", episodes[0], [1])
    drain(q)
    clock.advance(301)
    await ctrl.tick()
    (event,) = [e for e in drain(q) if e["type"] == "playback_stopped"]
    assert event["reason"] == "disconnected"


async def test_device_autoplay_gives_stopped_finished_then_started(conn, clock, fake, episodes):  # PB-3
    ctrl, q = await listening(conn, clock, fake)
    await ctrl.device_play(DEV, "A phone", episodes[0], [1])
    drain(q)
    await beat(ctrl, clock, DEV, "ended", EPISODE_S)
    events = drain(q)
    assert kinds(events) == ["playback_stopped", "playback_started"]
    assert events[0]["reason"] == "finished" and events[1]["episode_id"] == episodes[1]


async def test_a_tv_pick_replaces_a_device_session(conn, clock, fake, episodes):  # PB-8
    ctrl, q = await listening(conn, clock, fake)
    await ctrl.device_play(DEV, "A phone", episodes[0], [1])
    drain(q)
    await ctrl.play(episodes[1], [1])
    events = drain(q)
    assert [(e["type"], e.get("target"), e.get("reason")) for e in events] == [
        ("playback_stopped", "device", "replaced"), ("playback_started", "tv", None),
    ]


async def test_device_time_up_and_block(conn, clock, fake, episodes, kids):  # WT-11
    configure(conn, allowance_min=1, grace_min=0)
    ctrl, q = await listening(conn, clock, fake)
    await ctrl.device_play(DEV, "A phone", episodes[0], [1])
    await ctrl.device_play(DEV2, "B phone", episodes[0], [2])
    drain(q)
    await watch(ctrl, clock, DEV, 70)
    events = drain(q)
    up = [e for e in events if e["type"] == "time_up"]
    assert [e["profile_ids"] for e in up] == [[1]] and up[0]["reason"] == "allowance"
    assert [e["reason"] for e in events if e["type"] == "playback_stopped"] == ["time_up"]
    await ctrl.override("block", profile_ids=[2])
    assert [(e["type"], e.get("reason")) for e in drain(q)] == [
        ("override_applied", None), ("time_up", "blocked"), ("playback_stopped", "blocked"),
    ]


# --------------------------------------------------------------------------- overrides


async def test_override_fields_everyone_and_source_verbatim(conn, clock, fake, episodes, kids):  # HA-3, HA-7
    ctrl, q = await listening(conn, clock, fake)
    await ctrl.override("extra_minutes", 15, source="Home Assistant")
    (event,) = drain(q)
    assert event == {
        "type": "override_applied", "at": to_db(clock.now()), "kind": "extra_minutes", "value": 15,
        "profile_ids": [1, 2, 3], "source": "Home Assistant",
    }


async def test_override_source_none_is_null_on_the_wire(conn, clock, fake, episodes):  # HA-13: the admin pages
    ctrl, q = await listening(conn, clock, fake)
    await ctrl.override("clear")
    (event,) = drain(q)
    assert event["source"] is None and event["value"] is None
    assert json.loads(json.dumps(event))["source"] is None


@pytest.mark.parametrize("kind", ["unlimited", "block", "stop_now", "clear"])
async def test_override_value_is_null_unless_extra_minutes(conn, clock, fake, episodes, kids, kind):
    ctrl, q = await listening(conn, clock, fake)
    await ctrl.override(kind, 1 if kind != "clear" else None, profile_ids=[2, 1])
    (event,) = drain(q)
    assert (event["kind"], event["value"], event["profile_ids"]) == (kind, None, [1, 2])


async def test_a_rejected_override_emits_nothing(conn, clock, fake, episodes):
    ctrl, q = await listening(conn, clock, fake)
    with pytest.raises(ValueError):
        await ctrl.override("extra_minutes", 0)
    with pytest.raises(ValueError):
        await ctrl.override("block", profile_ids=[99])
    assert drain(q) == []


async def test_the_override_event_follows_the_timer_change(conn, clock, fake, episodes):  # HA-13
    ctrl, q = await listening(conn, clock, fake)
    state_q = ctrl.subscribe()
    seen = []
    publish = ctrl._event_bus.publish
    ctrl._event_bus.publish = lambda e: (seen.append(ctrl.timer.usage(1).blocked), publish(e))
    await ctrl.override("block")
    assert seen == [True]
    assert drain(state_q)[-1]["timer"]["profiles"][0]["blocked"] is True


# --------------------------------------------------------------------------- time_up and last_five edges


async def test_last_five_and_time_up_fire_once_not_per_tick(conn, clock, fake, episodes):  # HA-13, KA-8, KA-9
    configure(conn, allowance_min=7, grace_min=15)
    ctrl, q = await listening(conn, clock, fake)
    await play(ctrl, fake, episodes[0])
    drain(q)
    await run_for(ctrl, fake, clock, 430)
    events = drain(q)
    assert kinds(events) == ["last_five", "time_up"]
    assert events[0]["profile_ids"] == [1] and events[0]["remaining_s"] == pytest.approx(300, abs=1)
    assert events[1] == {"type": "time_up", "at": events[1]["at"], "profile_ids": [1], "reason": "allowance"}
    await run_for(ctrl, fake, clock, 60)  # still time up (finishing the episode): nothing repeats
    assert drain(q) == []


async def test_session_max_is_the_time_up_reason(conn, clock, fake, episodes):  # WT-3
    configure(conn, allowance_min=600, max_session_min=3, grace_min=15)
    ctrl, q = await listening(conn, clock, fake)
    await play(ctrl, fake, episodes[0])
    drain(q)
    await run_for(ctrl, fake, clock, 185)
    assert [(e["type"], e.get("reason")) for e in drain(q)] == [("time_up", "session_max")]


async def test_extra_minutes_re_arm_both_edges(conn, clock, fake, episodes):  # WT-7
    configure(conn, allowance_min=7, grace_min=15)
    ctrl, q = await listening(conn, clock, fake)
    await play(ctrl, fake, episodes[0])
    await run_for(ctrl, fake, clock, 430)
    assert kinds(drain(q)).count("time_up") == 1
    await ctrl.override("extra_minutes", 10)
    assert kinds(drain(q)) == ["override_applied"]  # false again, no event for that
    await run_for(ctrl, fake, clock, 600)
    events = [e for e in drain(q) if not e["type"].startswith("playback_")]  # episode 1 ended, autoplay went on
    assert kinds(events) == ["last_five", "time_up"]


async def test_the_daily_reset_re_arms(conn, clock, fake, episodes):  # WT-1
    configure(conn, allowance_min=7, grace_min=0)
    ctrl, q = await listening(conn, clock, fake)
    await play(ctrl, fake, episodes[0])
    await run_for(ctrl, fake, clock, 425)
    assert kinds(drain(q)).count("time_up") == 1
    clock.advance(timedelta(hours=13).total_seconds())  # past 04:00 local
    await ctrl.tick()
    assert drain(q) == []
    await play(ctrl, fake, episodes[1])
    drain(q)
    await run_for(ctrl, fake, clock, 425)
    events = drain(q)
    assert kinds(events) == ["last_five", "time_up", "playback_stopped"]


async def test_a_reset_while_watching_re_arms_without_an_event(conn, clock, fake, episodes):  # WT-1
    configure(conn, allowance_min=2, grace_min=15)
    conn.execute("UPDATE episode SET duration_s = 86400")
    fake.default_duration_s = 86400
    ctrl, q = await listening(conn, clock, fake)
    await play(ctrl, fake, episodes[0])
    await run_for(ctrl, fake, clock, 125)
    assert kinds(drain(q)).count("time_up") == 1
    clock.advance(timedelta(hours=13).total_seconds())
    await ctrl.tick()
    assert "time_up" not in kinds(drain(q))  # the value went false; that is not an event


async def test_start_up_in_a_time_up_state_emits_nothing(conn, clock, fake, episodes):
    """Decision: a group already time up (or in its last five) when the service starts emits no event. Events are
    edges seen by this process; the state (time_up, last_five) carries the level, and a restart must not replay
    what Home Assistant already heard."""
    configure(conn, allowance_min=7, grace_min=0)
    first = await make_controller(conn, clock, fake)
    await play(first, fake, episodes[0])
    await run_for(first, fake, clock, 430)
    first.persist(clock.now())
    assert not first.state()["timer"]["can_start"]

    second = CastController(conn, clock=clock, tz=TZ, media_base_url="http://tv.test:8080", secret=b"s", device=fake)
    q = second.subscribe_events()
    await second.start(run_loops=False)
    for _ in range(3):
        clock.advance(1)
        await second.tick()
    assert not second.state()["timer"]["can_start"] and drain(q) == []


async def test_a_pick_already_in_its_last_five_emits_no_last_five(conn, clock, fake, episodes):
    """Decision: edges are taken while a group plays, from the value the idle group already had."""
    configure(conn, allowance_min=7, grace_min=15)
    ctrl, q = await listening(conn, clock, fake)
    await play(ctrl, fake, episodes[0])
    await run_for(ctrl, fake, clock, 200)
    await ctrl.stop()
    drain(q)
    await play(ctrl, fake, episodes[1])
    await run_for(ctrl, fake, clock, 20)
    assert "last_five" not in kinds(drain(q))


# --------------------------------------------------------------------------- ordering, the bus and the endpoints


async def test_the_state_broadcast_precedes_its_events(conn, clock, fake, episodes):  # HA-13
    ctrl, q = await listening(conn, clock, fake)
    state_q = ctrl.subscribe()
    state_q.get_nowait()  # the initial snapshot
    order = []
    publish = ctrl._event_bus.publish

    def record(event):
        order.append((event["type"], state_q.qsize() and list(state_q._queue)[-1]["now_playing"]))
        publish(event)

    ctrl._event_bus.publish = record
    await play(ctrl, fake, episodes[0])
    assert order[0][0] == "playback_started" and order[0][1]["episode_id"] == episodes[0]
    await ctrl.stop()
    assert order[-1][0] == "playback_stopped" and order[-1][1] is None


async def test_nothing_queues_up_without_subscribers(conn, clock, fake, episodes):
    ctrl = await make_controller(conn, clock, fake)
    await play(ctrl, fake, episodes[0])
    await ctrl.override("block")
    assert ctrl._pending_events == []


async def test_two_subscribers_each_see_every_event_and_unsubscribe_works(conn, clock, fake, episodes):
    ctrl, q1 = await listening(conn, clock, fake)
    q2 = ctrl.subscribe_events()
    await ctrl.override("clear")
    assert len(drain(q1)) == len(drain(q2)) == 1
    ctrl.unsubscribe_events(q2)
    await ctrl.override("clear")
    assert len(drain(q1)) == 1 and q2.empty()


async def test_no_replay_for_a_late_subscriber(conn, clock, fake, episodes):
    ctrl, _ = await listening(conn, clock, fake)
    await ctrl.override("clear")
    assert ctrl.subscribe_events().empty()


def test_the_bus_drops_the_oldest_and_logs_once_per_episode(caplog):
    bus = EventBus(maxsize=3)
    q = bus.subscribe()
    with caplog.at_level(logging.WARNING, logger="tellybox.cast.events"):
        for i in range(10):
            bus.publish({"type": "x", "n": i})
        assert [e["n"] for e in drain(q)] == [7, 8, 9]
        assert len(caplog.records) == 1  # one line for the whole overflow, not one per dropped event
        bus.publish({"type": "x", "n": 10})  # the reader caught up: a new overflow is a new episode
        for i in range(11, 16):
            bus.publish({"type": "x", "n": i})
        assert len(caplog.records) == 2
    assert [e["n"] for e in drain(q)] == [13, 14, 15]


def test_a_slow_subscriber_does_not_hold_up_another():
    bus = EventBus(maxsize=2)
    slow, fast = bus.subscribe(), bus.subscribe()
    for i in range(5):
        bus.publish({"n": i})
        assert fast.get_nowait() == {"n": i}
    assert [e["n"] for e in drain(slow)] == [3, 4]


def test_the_default_queue_is_100():
    assert typed.QUEUE_SIZE == 100 and EventBus().subscribe().maxsize == 100


def route(env, path):  # noqa: F811
    return next(r for r in env["client"]._transport.app.routes if getattr(r, "path", None) == path)


async def test_the_typed_events_endpoint_frames_and_cleans_up(env, monkeypatch):  # noqa: F811
    monkeypatch.setattr(cast_api, "SSE_KEEPALIVE_S", 0.05)
    ctrl = env["ctrl"]
    response = await route(env, "/typed-events").endpoint()
    assert response.media_type == "text/event-stream" and response.headers["cache-control"] == "no-cache"
    stream = response.body_iterator
    assert await stream.__anext__() == ": keepalive\n\n"
    await ctrl.override("block", source="Home Assistant")
    frame = await stream.__anext__()
    assert frame.startswith("data: ") and frame.endswith("\n\n")
    event = json.loads(frame[len("data: "):])
    assert event["type"] == "override_applied" and event["source"] == "Home Assistant"
    assert ctrl._event_bus
    await stream.aclose()
    assert not ctrl._event_bus  # the subscriber went with the connection


async def test_the_state_stream_is_unchanged(env):  # noqa: F811
    ctrl = env["ctrl"]
    typed_q = ctrl.subscribe_events()
    response = await route(env, "/events").endpoint()
    stream = response.body_iterator
    first = await stream.__anext__()
    assert first == f"data: {json.dumps(ctrl.state())}\n\n"
    await ctrl.override("block")
    frame = await stream.__anext__()
    payload = json.loads(frame[len("data: "):])
    assert "type" not in payload and payload["timer"]["profiles"][0]["blocked"] is True  # a snapshot, never an event
    assert drain(typed_q)[0]["type"] == "override_applied"
    await stream.aclose()
