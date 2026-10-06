# Step 21: typed events on the admin stream (v13, HA-13)

Status: proposed by the owner on 2026-10-06 (PRD steps 19 to 21), approved to build the same day. Branch: `step21/typed-events`.

The Tellybox side of the Home Assistant integration's typed events. Until now the integration derives events by diffing state; this step lets Tellybox say why playback stopped and who applied an override. Release order: Tellybox, then pytellybox (an event model and a typed stream), then the integration, which switches to typed events when the capability is present and keeps its diff as the fallback.

## Decisions (owner, 2026-10-06)

- **Opt-in on the existing endpoint:** `GET /api/admin/events?typed=1` plus a `typed_events` capability. Without the option the stream is byte-identical, so older clients keep working.
- **Events are advisory edges:** not stored, not replayed. The state remains the source of truth.
- **Where events come from:** the cast service emits playback, timer and override events on a new internal `GET /typed-events` (the existing `/events` must stay snapshot-only, because the web service's `CastClient.events()` parses every frame as a state). The web service emits `inbox_item_arrived` and `download_ready` by watching the database, like `watch_inbox`.
- `download_ready` carries only counts (`held_ready`, `new_ready`).

## Contract

`docs/admin-api.md`, "Typed events", and `docs/cast-api.md`, "Typed events", are the contract (catalog, envelope, ordering, slow clients).

## What exists

- `tellybox/cast/controller.py` has every transition point: `_start_episode`, `_end_current(reason: EndReason)`, the in-app `_end_device` path, `override(kind, value, profile_ids, source)`, `tick()` and `_apply_decision` (time up and last five), and `_broadcast()` (deduplicated snapshots). There is no event queue.
- `tellybox/web/hub.py` relays the cast `/events` stream to `sse_stream`; `tellybox/web/cast_client.py` parses every frame as a state.
- `tellybox/web/api/state.py` has `watch_inbox`; `router.py` has `CAPABILITIES` and `GET /api/admin/events`.

## Tasks

- **T1 (cast)**: `tellybox/cast/events.py` (the builders and the bus), the controller emission points, `GET /typed-events`; `tests/cast/test_typed_events.py`.
- **T2 (web)**: `tellybox/web/events.py` (`EventHub`), `CastClient.typed_events()`, `?typed=1` on the admin stream, the inbox and download watchers, the capability; `tests/web/api/test_typed_events.py`, the capability assertion in `tests/web/api/test_auth.py`.
- Docs: `docs/PROGRESS.md`.

## Tests

One event per transition (no per-tick repeats); each stop reason; autoplay gives stopped (finished) then started; an extra-minutes override re-arms `time_up`; `source` null; the existing `/events` output unchanged; the plain admin stream byte-identical without `typed=1` (regression); states arrive before their events; read scope suffices (401 and 403); a cast outage doesn't break the stream and inbox events still flow; a slow subscriber doesn't block others.
