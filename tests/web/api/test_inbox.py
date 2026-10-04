"""The inbox sensor (HA-9, A-36): AdminState.inbox, the capability, and the push to SSE listeners."""

from __future__ import annotations

import asyncio
import json
from datetime import timedelta

from tellybox import subscriptions
from tellybox.db import to_db
from tellybox.web import hub as hub_module
from tellybox.web.api import state as state_module
from tests.channel_fakes import FakeChannelLister
from tests.web.api.test_events import events_endpoint, next_chunk
from tests.web.conftest import NOW, FakeCast

STATE = "/api/admin/state"
EMPTY = {"pending": 0, "unhealthy": 0, "latest_received_at": None}


def subscribe(api, handle="@kids", channel="UCkids"):
    lister = FakeChannelLister()
    lister.add_channel(channel, "Kids", handle=handle, videos=["old0"])
    sub = subscriptions.subscribe(api.conn, lister, f"https://www.youtube.com/{handle}", now=NOW)
    return lister, sub


def upload(api, lister, sub, *ids, at=NOW):
    for youtube_id in ids:
        lister.upload("UCkids", youtube_id)
    assert subscriptions.check(api.conn, lister, sub.id, now=at).ok


def test_info_lists_the_inbox_capability(client):
    assert "inbox" in client.get("/api/info").json()["capabilities"]


def test_state_carries_the_inbox(client, api, reader):
    assert client.get(STATE, headers=reader).json()["inbox"] == EMPTY
    lister, sub = subscribe(api)
    upload(api, lister, sub, "newA", "newB")
    inbox = client.get(STATE, headers=reader).json()["inbox"]
    assert inbox == {"pending": 2, "unhealthy": 0, "latest_received_at": to_db(NOW)}


def test_pending_counts_paused_subscriptions_and_ignores_decided_items(client, api, reader):
    lister, sub = subscribe(api)
    upload(api, lister, sub, "newA", "newB", "newC")
    subscriptions.pause(api.conn, sub.id)
    item = api.conn.execute("SELECT id FROM inbox_item WHERE youtube_id = 'newA'").fetchone()[0]
    subscriptions.reject(api.conn, item, now=NOW)
    assert client.get(STATE, headers=reader).json()["inbox"]["pending"] == 2


def test_unhealthy_after_seven_days_of_failures(client, api, reader):
    _lister, sub = subscribe(api)
    api.conn.execute("UPDATE subscription SET failing_since = ? WHERE id = ?", (to_db(NOW - timedelta(days=6)), sub.id))
    assert client.get(STATE, headers=reader).json()["inbox"]["unhealthy"] == 0
    api.clock.advance(days=2)
    assert client.get(STATE, headers=reader).json()["inbox"]["unhealthy"] == 1


def test_the_sensor_answers_while_the_cast_service_is_down(client, api, reader):
    lister, sub = subscribe(api)
    api.cast.mode = "down"
    cold = client.get(STATE, headers=reader)
    assert cold.status_code == 200 and cold.json()["tv"]["connection"] == "unreachable"
    assert cold.json()["inbox"]["pending"] == 0
    upload(api, lister, sub, "newA")  # no hub refresh in between: the endpoint reads the database itself
    assert client.get(STATE, headers=reader).json()["inbox"]["pending"] == 1


def test_the_overrides_response_carries_the_inbox_too(client, api, controller):
    lister, sub = subscribe(api)
    upload(api, lister, sub, "newA")
    body = client.post("/api/admin/overrides/extra", json={"minutes": 5}, headers=controller).json()
    assert body["inbox"]["pending"] == 1


async def test_first_event_has_the_inbox(api):
    lister, sub = subscribe(api)
    upload(api, lister, sub, "newA")
    api.app.state.admin_hub.refresh()  # what the inbox watcher does within 2 s of a change
    response = await events_endpoint(api.app)()
    first = await next_chunk(response.body_iterator)
    assert json.loads(first[len("data: "):])["inbox"]["pending"] == 1
    await response.body_iterator.aclose()


async def test_an_inbox_change_reaches_listeners_within_a_couple_of_seconds(api):
    lister, sub = subscribe(api)
    hub = hub_module.KidHub(FakeCast(), lambda s: {"inbox": subscriptions.inbox_counts(api.conn, NOW)},
                            initial=lambda: {"inbox": subscriptions.inbox_counts(api.conn, NOW)})
    q = hub.subscribe()
    q.get_nowait()
    watcher = asyncio.create_task(state_module.watch_inbox(api.conn, lambda: NOW, hub.refresh, 0.01))
    try:
        await asyncio.sleep(0.05)
        assert q.empty()  # nothing changed, nothing pushed
        upload(api, lister, sub, "newA")
        assert (await asyncio.wait_for(q.get(), 1))["inbox"]["pending"] == 1
        item = api.conn.execute("SELECT id FROM inbox_item").fetchone()[0]
        subscriptions.reject(api.conn, item, now=NOW)
        assert (await asyncio.wait_for(q.get(), 1))["inbox"]["pending"] == 0
    finally:
        watcher.cancel()
        await asyncio.gather(watcher, return_exceptions=True)


async def test_the_watcher_survives_a_failing_read(api):
    calls = []

    def flaky_now():
        calls.append(1)
        if len(calls) == 2:
            raise RuntimeError("database is locked")
        return NOW

    hub = hub_module.KidHub(FakeCast(), lambda s: {})
    watcher = asyncio.create_task(state_module.watch_inbox(api.conn, flaky_now, hub.refresh, 0.01))
    try:
        await asyncio.sleep(0.1)
        assert len(calls) > 3 and not watcher.done()
    finally:
        watcher.cancel()
        await asyncio.gather(watcher, return_exceptions=True)


async def test_lifespan_runs_the_inbox_watcher(api):
    async with api.app.router.lifespan_context(api.app):
        tasks = {t.get_name() for t in asyncio.all_tasks()}
        assert "inbox-watch" in tasks
    assert "inbox-watch" not in {t.get_name() for t in asyncio.all_tasks() if not t.done()}
