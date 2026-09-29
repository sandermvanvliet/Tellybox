"""GET /api/admin/events (HA-3) and the admin hub."""

from __future__ import annotations

import asyncio
import json

from tellybox.web import hub as hub_module
from tellybox.web.api import router as router_module
from tests.web.conftest import FakeCast, cast_state, playing


def events_endpoint(app):
    """The handler itself: TestClient buffers a streaming response, so read its body iterator directly."""
    routes = [r for top in app.routes for r in (getattr(top, "original_router", top).routes
                                                if hasattr(top, "original_router") else [top])]
    return next(r.endpoint for r in routes if getattr(r, "path", None) == "/api/admin/events")


async def next_chunk(body, timeout=2.0):
    return await asyncio.wait_for(body.__anext__(), timeout)


async def test_first_event_is_the_current_admin_state(api):
    response = await events_endpoint(api.app)()
    assert response.media_type == "text/event-stream"
    body = response.body_iterator
    first = await next_chunk(body)
    assert first.startswith("data: ")
    state = json.loads(first[len("data: "):])
    assert state["api"] == 1 and [p["name"] for p in state["profiles"]] == ["Mila", "Noor"]
    await body.aclose()


async def test_keepalive_when_nothing_changes(api, monkeypatch):
    monkeypatch.setattr(router_module, "SSE_KEEPALIVE_S", 0.02)
    body = (await events_endpoint(api.app)()).body_iterator
    await next_chunk(body)  # the state
    assert await next_chunk(body) == ": keepalive\n\n"
    await body.aclose()


async def test_changes_are_relayed_with_the_admin_reduction(api):
    admin_hub = api.app.state.admin_hub
    api.cast.streams.append([cast_state(remaining_s=111, profiles=[]),
                             cast_state(now_playing=playing(api.ids.b1, api.ids.bravo), remaining_s=99)])
    body = (await events_endpoint(api.app)()).body_iterator
    await next_chunk(body)
    admin_hub.start()
    try:
        seen = [json.loads((await next_chunk(body))[6:]) for _ in range(2)]
    finally:
        await admin_hub.stop()
        await body.aclose()
    assert seen[0]["group"]["remaining_s"] == 111
    assert seen[1]["now_playing"]["show"] == "Bravo"


async def test_lifespan_starts_and_stops_the_admin_hub(api):
    assert api.app.state.admin_hub is not api.app.state.hub
    async with api.app.router.lifespan_context(api.app):
        assert api.app.state.admin_hub._task is not None
        assert api.app.state.hub._task is not None
    assert api.app.state.admin_hub._task is None


# ------------------------------------------------------------------ KidHub generalisation


def reduce(s):
    return {"tv": "ok", "n": s.get("n")}


async def run_a_while(hub, seconds):
    hub.start()
    try:
        await asyncio.sleep(seconds)
    finally:
        await hub.stop()


async def test_refresh_re_reduces_the_last_cast_state():
    counter = {"n": 0}

    def reducing(s):
        counter["n"] += 1
        return {"tv": "ok", "tick": counter["n"]}

    cast = FakeCast()
    cast.streams.append([{"x": 1}])
    hub = hub_module.KidHub(cast, reducing, refresh_s=0.02)
    q = hub.subscribe()
    await run_a_while(hub, 0.15)
    ticks = []
    while not q.empty():
        ticks.append(q.get_nowait())
    assert sum(1 for t in ticks if "tick" in t) >= 3  # the event, then refreshes of the same cast state


async def test_no_refresh_by_default():
    cast = FakeCast()
    cast.streams.append([{"x": 1}])
    calls = []
    hub = hub_module.KidHub(cast, lambda s: calls.append(1) or {"n": len(calls)})
    await run_a_while(hub, 0.1)
    assert len(calls) == 1


async def test_custom_unreachable_is_used_when_the_cast_service_drops():
    cast = FakeCast()
    cast.streams.append([{"x": 1}])
    hub = hub_module.KidHub(cast, lambda s: {"tv": "ok", "keep": 1}, unreachable=lambda st: {**st, "tv": "gone"},
                            min_backoff=0.01, max_backoff=0.01)
    await run_a_while(hub, 0.1)
    assert hub.state == {"tv": "gone", "keep": 1}


async def test_kid_hub_defaults_are_unchanged():
    hub = hub_module.KidHub(FakeCast(), reduce)
    assert hub.state is hub_module.INITIAL_STATE
    assert hub.refresh_s is None
