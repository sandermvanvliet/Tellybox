"""Typed events on the admin stream (HA-13): `?typed=1`, the EventHub, the cast relay, the database watchers."""

from __future__ import annotations

import asyncio
import json
import httpx
import pytest
from starlette.requests import Request

from tellybox import subscriptions
from tellybox.db import to_db
from tellybox.web import hub as hub_module
from tellybox.web.api import router as router_module
from tellybox.web.api import state as state_module
from tellybox.web.api.guard import TokenGuard
from tellybox.web.cast_client import CastClient, CastUnavailable
from tellybox.web.events import EventHub
from tests.web.api.test_events import next_chunk
from tests.web.api.test_inbox import subscribe, upload
from tests.web.conftest import NOW, FakeCast

STARTED = {"type": "playback_started", "at": "2026-10-06T18:02:11.000+00:00", "profile_ids": [1], "episode_id": 7}
STOPPED = {"type": "playback_stopped", "at": "2026-10-06T18:03:11.000+00:00", "profile_ids": [1], "reason": "stopped"}


def events_endpoint(app):
    routes = [r for top in app.routes for r in (getattr(top, "original_router", top).routes
                                                if hasattr(top, "original_router") else [top])]
    return next(r.endpoint for r in routes if getattr(r, "path", None) == "/api/admin/events")




async def typed_body(api):
    return (await events_endpoint(api.app)(typed="1")).body_iterator


class TypedCast(FakeCast):
    """A fake cast service with a scripted /typed-events: each item is a list of events or an exception."""

    def __init__(self) -> None:
        super().__init__()
        self.typed_streams: list = []
        self.typed_calls = 0

    async def typed_events(self):
        self.typed_calls += 1
        item = self.typed_streams.pop(0) if self.typed_streams else CastUnavailable("no more scripted streams")
        if isinstance(item, BaseException):
            raise item
        for e in item:
            yield e
        await asyncio.sleep(3600)  # a live stream stays open


def parse_named(chunk: str) -> tuple[str, dict]:
    head, data = chunk.rstrip("\n").split("\n")
    assert head.startswith("event: ") and data.startswith("data: ")
    return head[7:], json.loads(data[6:])


# ------------------------------------------------------------------ the stream


def test_info_lists_typed_events_after_history(client):
    caps = client.get("/api/info").json()["capabilities"]
    assert "typed_events" in caps and caps.index("typed_events") == caps.index("history") + 1


async def test_plain_stream_is_unchanged_even_with_typed_events_flowing(api):
    """HA-13 regression: no `typed=1` means state frames only, byte-identical to sse_stream."""
    plain = (await events_endpoint(api.app)()).body_iterator
    reference = hub_module.sse_stream(api.app.state.admin_hub, 0.02)
    first = await next_chunk(plain)
    assert first == await next_chunk(reference)
    assert first.startswith("data: ") and first.endswith("\n\n")
    api.app.state.event_hub.publish(STARTED)
    api.app.state.admin_hub.publish({**api.app.state.admin_hub.state, "marker": 1})
    chunk = await next_chunk(plain)
    assert chunk == f"data: {json.dumps(api.app.state.admin_hub.state)}\n\n" and "marker" in chunk
    await plain.aclose()
    await reference.aclose()


async def test_typed_stream_sends_the_state_first_then_named_frames(api):
    body = await typed_body(api)
    first = await next_chunk(body)
    assert first.startswith("data: ") and json.loads(first[6:])["api"] == 1
    api.app.state.event_hub.publish(STARTED)
    api.app.state.event_hub.publish(STOPPED)
    chunk = await next_chunk(body)
    assert chunk == f"event: playback_started\ndata: {json.dumps(STARTED)}\n\n"
    assert parse_named(await next_chunk(body)) == ("playback_stopped", STOPPED)
    await body.aclose()


async def test_typed_stream_keepalive(api, monkeypatch):
    monkeypatch.setattr(router_module, "SSE_KEEPALIVE_S", 0.02)
    body = await typed_body(api)
    await next_chunk(body)
    assert await next_chunk(body) == ": keepalive\n\n"
    await body.aclose()


async def test_an_event_follows_the_state_that_reflects_it(api):
    admin_hub, event_hub = api.app.state.admin_hub, api.app.state.event_hub
    body = await typed_body(api)
    await next_chunk(body)
    # the event overtakes its state on the way in: it must still be sent after it
    event_hub.publish(STARTED)
    await asyncio.sleep(0)
    admin_hub.publish({**admin_hub.state, "marker": 2})
    first, second = await next_chunk(body), await next_chunk(body)
    assert first.startswith("data: ") and '"marker": 2' in first
    assert second.startswith("event: playback_started")
    await body.aclose()


async def test_disconnect_unsubscribes(api):
    event_hub = api.app.state.event_hub
    body = await typed_body(api)
    await next_chunk(body)
    assert len(event_hub._subscribers) == 1
    await body.aclose()
    assert not event_hub._subscribers


def test_typed_stream_needs_a_token(client):
    assert client.get("/api/admin/events?typed=1").status_code == 401


@pytest.mark.parametrize("secret", ["read", "control"])
async def test_read_scope_or_better_is_enough(api, secret):
    token = getattr(api, secret)
    req = Request({"type": "http", "method": "GET", "path": "/", "query_string": b"typed=1",
                   "headers": [(b"authorization", f"Bearer {token}".encode())]})
    assert await TokenGuard(api.conn, api.clock, "read")(req)


async def test_an_unknown_typed_value_gets_the_plain_stream(api):
    body = (await events_endpoint(api.app)(typed="0")).body_iterator
    api.app.state.event_hub.publish(STARTED)
    await next_chunk(body)
    with pytest.raises(TimeoutError):
        await next_chunk(body, 0.1)
    await body.aclose()


# ------------------------------------------------------------------ the hub


async def test_slow_subscriber_does_not_block_another_and_drops_oldest(caplog):
    hub = EventHub(queue_size=3)
    slow, fast = hub.subscribe(), hub.subscribe()
    with caplog.at_level("WARNING"):
        for n in range(10):
            hub.publish({"type": "t", "n": n})
            assert fast.get_nowait()["n"] == n  # the fast one keeps up
    assert [slow.get_nowait()["n"] for _ in range(3)] == [7, 8, 9]  # the slow one lost the oldest
    assert sum("slow" in r.message for r in caplog.records) == 1  # logged once per episode
    hub.unsubscribe(slow)
    hub.publish({"type": "t", "n": 10})
    assert slow.empty() and fast.get_nowait()["n"] == 10


async def test_overflow_is_logged_again_after_the_subscriber_recovers(caplog):
    hub = EventHub(queue_size=1)
    q = hub.subscribe()
    with caplog.at_level("WARNING"):
        for n in range(3):
            hub.publish({"type": "t", "n": n})
        q.get_nowait()
        hub.publish({"type": "t", "n": 3})  # room again: the episode is over
        hub.publish({"type": "t", "n": 4})
    assert sum("slow" in r.message for r in caplog.records) == 2


async def test_a_new_subscriber_gets_no_replay():
    hub = EventHub()
    hub.publish(STARTED)
    assert hub.subscribe().empty()


async def run_a_while(hub, seconds):
    hub.start()
    try:
        await asyncio.sleep(seconds)
    finally:
        await hub.stop()


async def test_the_relay_publishes_what_the_cast_service_emits():
    cast = TypedCast()
    cast.typed_streams.append([STARTED, STOPPED])
    hub = EventHub(cast)
    q = hub.subscribe()
    await run_a_while(hub, 0.1)
    assert [q.get_nowait(), q.get_nowait()] == [STARTED, STOPPED]


async def test_the_relay_survives_an_outage_and_reconnects(caplog):
    cast = TypedCast()
    cast.typed_streams += [CastUnavailable("down"), CastUnavailable("still down"), [STARTED]]
    hub = EventHub(cast, min_backoff=0.01, max_backoff=0.02)
    q = hub.subscribe()
    with caplog.at_level("WARNING"):
        await run_a_while(hub, 0.3)
    assert q.get_nowait() == STARTED
    assert sum("unreachable" in r.message for r in caplog.records) == 1  # once per outage


async def test_the_relay_never_crashes_on_an_unexpected_error():
    cast = TypedCast()
    cast.typed_streams += [RuntimeError("boom"), [STARTED]]
    hub = EventHub(cast, min_backoff=0.01, max_backoff=0.02)
    q = hub.subscribe()
    await run_a_while(hub, 0.2)
    assert q.get_nowait() == STARTED


async def test_a_cast_service_without_typed_events_does_not_crash_the_hub():
    hub = EventHub(FakeCast(), min_backoff=0.01, max_backoff=0.02)  # no typed_events(): an old or fake cast
    await run_a_while(hub, 0.1)
    assert hub._task is None


async def test_cast_outage_does_not_break_the_stream_and_database_events_still_flow(api, monkeypatch):
    cast = api.cast
    api.app.state.event_hub.cast = cast
    cast.mode = "down"
    body = await typed_body(api)
    await next_chunk(body)
    lister, sub = subscribe(api)
    monkeypatch.setattr(router_module, "SSE_KEEPALIVE_S", 0.02)
    publish = api.app.state.event_hub.publish
    task = asyncio.create_task(state_module.watch_typed_events(api.conn, api.clock.now, publish, 0.01))
    await asyncio.sleep(0.03)
    upload(api, lister, sub, "newA")
    chunks = []
    while not any(c.startswith("event: inbox_item_arrived") for c in chunks):
        chunks.append(await next_chunk(body))
    task.cancel()
    await body.aclose()
    name, event = parse_named(chunks[-1])
    assert event["pending"] == 1 and event["new_items"] == 1


async def test_lifespan_runs_the_relay_and_the_watchers(api):
    event_hub = api.app.state.event_hub
    async with api.app.router.lifespan_context(api.app):
        assert event_hub._task is not None
    assert event_hub._task is None


# ------------------------------------------------------------------ the database watchers


async def watch(api, interval=0.01):
    published: list[dict] = []
    task = asyncio.create_task(state_module.watch_typed_events(api.conn, api.clock.now, published.append, interval))
    await asyncio.sleep(0.03)  # the baseline reading
    return published, task


async def settle():
    await asyncio.sleep(0.05)


def add_source(api, n: int, *, publish="hold", status="ready") -> int:
    """A source video; with the defaults it is a held download that finished (HA-2 `held_ready`)."""
    api.conn.execute(
        "INSERT INTO source_video (youtube_id, url, title, publish, status, created_at, updated_at)"
        " VALUES (?, 'u', 't', ?, ?, ?, ?)", (f"src{n}", publish, status, to_db(NOW), to_db(NOW)))
    return api.conn.execute("SELECT id FROM source_video WHERE youtube_id = ?", (f"src{n}",)).fetchone()[0]


async def test_inbox_event_fires_once_per_advance_and_not_at_startup(api):
    lister, sub = subscribe(api)
    upload(api, lister, sub, "early")  # already there at start-up: the baseline
    published, task = await watch(api)
    await settle()
    assert published == []
    api.clock.advance(minutes=5)
    upload(api, lister, sub, "newA", "newB", at=api.clock.now())
    await settle()
    assert len(published) == 1
    assert published[0] == {"type": "inbox_item_arrived", "at": to_db(api.clock.now()), "pending": 3,
                            "new_items": 2, "latest_received_at": to_db(api.clock.now())}
    await settle()
    assert len(published) == 1  # nothing new: no repeat
    task.cancel()


async def test_inbox_event_not_on_a_decrease_and_new_items_is_at_least_one(api):
    lister, sub = subscribe(api)
    upload(api, lister, sub, "a", "b")
    published, task = await watch(api)
    item = api.conn.execute("SELECT id FROM inbox_item WHERE youtube_id = 'a'").fetchone()[0]
    subscriptions.reject(api.conn, item, now=NOW)  # pending falls
    await settle()
    assert published == []
    api.clock.advance(minutes=1)
    upload(api, lister, sub, "c", at=api.clock.now())
    api.conn.execute("UPDATE inbox_item SET status = 'rejected' WHERE youtube_id = 'b'")  # delta 0 within the poll
    await settle()
    assert [e["new_items"] for e in published] == [1]
    task.cancel()


async def test_download_ready_fires_when_the_held_ready_count_rises(api):
    published, task = await watch(api)
    assert published == []
    first = add_source(api, 0)
    await settle()
    assert [(e["type"], e["held_ready"], e["new_ready"]) for e in published] == [("download_ready", 1, 1)]
    assert published[0]["at"] == to_db(api.clock.now())
    add_source(api, 1)
    add_source(api, 2)
    await settle()
    assert published[-1]["held_ready"] == 3 and published[-1]["new_ready"] == 2
    api.conn.execute("UPDATE source_video SET publish = 'publish' WHERE id = ?", (first,))  # a decrease: silent
    await settle()
    assert len(published) == 2
    add_source(api, 3, status="queued")  # not ready yet: not counted
    await settle()
    assert len(published) == 2
    task.cancel()


async def test_download_ready_is_not_fired_for_the_state_at_startup(api):
    add_source(api, 0)
    published, task = await watch(api)
    await settle()
    assert published == []
    task.cancel()


# ------------------------------------------------------------------ CastClient.typed_events


def client_for(body: bytes, status: int = 200) -> CastClient:
    def handler(r: httpx.Request) -> httpx.Response:
        assert r.url.path == "/typed-events"
        return httpx.Response(status, content=body, headers={"content-type": "text/event-stream"})
    return CastClient("http://cast.test", transport=httpx.MockTransport(handler))


async def test_typed_events_parses_frames_and_skips_junk():
    body = (b": keepalive\n\n"
            b"data: " + json.dumps(STARTED).encode() + b"\n\n"
            b"data: {not json\n\n"
            b"data: [1, 2]\n\n"
            b'data: {"no_type": 1}\n\n'
            b"event: ignored\ndata: " + json.dumps(STOPPED).encode() + b"\n\n")
    assert [e async for e in client_for(body).typed_events()] == [STARTED, STOPPED]


async def test_typed_events_raises_cast_unavailable():
    with pytest.raises(CastUnavailable):
        async for _ in client_for(b"", 404).typed_events():
            pass

    def broken(r):
        raise httpx.ConnectError("refused")

    with pytest.raises(CastUnavailable):
        async for _ in CastClient("http://cast.test", transport=httpx.MockTransport(broken)).typed_events():
            pass


async def test_events_is_unchanged_by_typed_frames():
    body = b"data: " + json.dumps({"tv": "ok"}).encode() + b"\n\n"

    def handler(r):
        assert r.url.path == "/events"
        return httpx.Response(200, content=body)

    c = CastClient("http://cast.test", transport=httpx.MockTransport(handler))
    assert [s async for s in c.events()] == [{"tv": "ok"}]
