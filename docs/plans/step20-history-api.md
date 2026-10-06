# Step 20: daily history API (v12, HA-12, A-38)

Status: proposed by the owner on 2026-10-06 (PRD steps 19 to 21), approved to build the same day. Branch: `step20/history-api`.

The Tellybox side of the Home Assistant integration's yesterday and weekly-average sensors. Release order: Tellybox, then pytellybox (a client method and models), then the integration.

## Decisions (owner, 2026-10-06)

- One new read-scope endpoint, `GET /api/admin/history?days=N&profile_ids=…`, and a `history` capability in `/api/info`.
- Titles of the last watched episode are visible to any `read` token (A-38). No backfill of Home Assistant's long-term statistics.
- It reads the database only (like HA-9 and HA-10), so it answers while the cast service is down and is outside HA-8.

## Contract

`docs/admin-api.md`, "History", is the contract. In code: `tellybox/history.py`, `usage_history(conn, now, tz, reset_time, days=7, profile_ids=None) -> UsageHistory` (frozen dataclasses `UsageHistory`, `ProfileUsage`, `UsageDay`, `LastWatched`).

## What exists

- `daily_usage(profile_id, day, seconds_used, extra_min, unlimited, blocked)` is the timer's per-day truth; `watch_session` and `watch_session_profile` hold sessions (with `target`); `purge.py` keeps 21 days (AD-5).
- `history_days()` in this module feeds the admin History page and buckets by timer day with `day_for`.
- `CAPABILITIES` in `tellybox/web/api/router.py`; the token guard in `tellybox/web/api/guard.py`; `inbox` is the precedent for a database-only read.

## Tasks

- **T1** `usage_history` and its tests (`tests/test_history.py`).
- **T2** the route, the capability, the exact-list assertion in `tests/web/api/test_auth.py`, `tests/web/api/test_history.py`.
- Docs: `docs/admin-api.md`, `docs/PROGRESS.md`.

## Tests

A missing day gives zeros; a day boundary at the reset time (a session at 03:59 and at 04:01); two profiles with one shared session; a deleted episode; an open session; `days` bounds; `profile_ids` filtering and an unknown id; 422, 401 and 403 cases; works with the cast service down; the capability is listed.
