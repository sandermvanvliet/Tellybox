# Step 9 handoff: subagent briefs

These briefs go with `docs/plans/step9-ha-api.md`. Three subagents run in parallel on **Sonnet**, each in its own git worktree off the part 0 contract commit on `step9/ha-api`. The controller writes the contract first, then merges A, B and C in that order and reviews.

## Rules for every subagent

- **Base commit:** start by checking that your HEAD contains the contract commit (`git log --oneline -5` shows "Step 9 contract"). If it doesn't, run `git merge --ff-only step9/ha-api` first.
- **Read first:** `CLAUDE.md`, `docs/plans/step9-ha-api.md` (your part and part 0), `docs/admin-api.md`, `docs/cast-api.md` and the contract files named in your brief. The PRD (`docs/PRD.md`) is the source of truth. Cite the requirement IDs (HA-1..HA-8, WT-*, AD-*) in tests and comments where the surrounding code does.
- **TDD:** write the tests first, then the code. No network and no real Chromecast: use the fake clock, `tellybox/cast/fake.py` and the web tests' `FakeCast`.
- **Running tests:** use the main checkout's venv from your worktree root: `/home/sander/Projects/Tellybox/.venv/bin/python -m pytest -q`. The full suite must pass before you report.
- **Scope:** stay inside your files. If the contract is wrong or missing something, don't change it on your own: work around it minimally and report it.
- **Style:** match the surrounding code (naming, comment density, idioms). No new dependencies.
- **Security:** never log a token or put one in an error message.
- **Private details:** keep them out. No hostnames, IPs or internal domains; the repository is public.
- **Commits:** commit in your worktree with a clear message ending in `Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>`. Don't push, and don't open a PR.
- **Report back:**
  - what you built;
  - the test count before and after;
  - any contract gaps;
  - anything you're unsure about.

## Subagent A: cast service

**Files:** `tellybox/cast/`, `tellybox/store.py` (`log_override` only), `tests/cast/`, `tests/timer/`.

**Contract to read:** `docs/cast-api.md` ("Commands" and the state), migration `007_api.sql` (`override_log.source`).

**Tasks**
1. `POST /overrides` takes a body `{kind, value?, profile_ids?, profile_id?, source?}`.
   - `kind` is one of `extra_minutes`, `unlimited`, `block`, `stop_now` or `clear`.
   - `profile_ids` holds 1–20 ids; with neither `profile_ids` nor `profile_id`, the override applies to every profile.
   - `source` is at most 64 characters.
   - Unknown profile ids give a 422, and so does sending both `profile_id` and `profile_ids`.
2. `CastController.override(kind, value=None, profile_ids=None, source=None)`.
   - `clear` sets unlimited and blocked to false for each target, and logs one `clear` row per profile.
   - `stop_now` ignores the profiles, as it does today.
   - `store.log_override(..., source=None)` writes the new column.
3. `state()` gains:
   - `timer.next_reset`: ISO UTC, the timer's next daily reset;
   - `profiles[].session_elapsed_s`: from `ProfileStatus`, or null when the profile has no session.

   Keep every existing field.

**Tests:**
- an override for a subset of profiles leaves the others untouched;
- `clear` after unlimited and block;
- `source` is stored in `override_log`;
- an unknown profile gives a 422;
- `profile_id` still works;
- the new state fields, with the fake clock across a reset.

## Subagent B: JSON API and hub

**Files:**
- new: the `tellybox/web/api/` package (router, `TokenGuard`, the state builder) and `tellybox/web/overrides.py`;
- `tellybox/web/hub.py`, `tellybox/web/app.py`;
- `tellybox/jobs.py` and `tellybox/library.py` (count helpers only);
- `tellybox/web/admin/dashboard.py` (only the `POST /admin/overrides` handler, switched to `apply_override`);
- `tests/web/api/` (new), plus `tests/web/test_hub*.py` if the hub changes need tests.

Don't touch `tellybox/web/admin/templates/`, `history.py` or the locale files.

**Contract to read:** `docs/admin-api.md` (the whole thing), `tellybox/api_tokens.py`, and `CastClient.override` in `tellybox/web/cast_client.py`.

**Tasks**
1. `TokenGuard(conn, clock, scope)`, a FastAPI dependency.
   - It reads `Authorization: Bearer <secret>` and calls `api_tokens.authenticate`.
   - A missing, unknown or revoked token gets a 401 with `WWW-Authenticate: Bearer` and `{"detail": "unauthorized"}`.
   - The wrong scope gets a 403 with `{"detail": "forbidden"}`.
   - It sets `request.state.api_token`.
   - There is no Origin check, because no cookie is involved.
2. `tellybox/web/overrides.py`: `async apply_override(cast, kind, value=None, profile_ids=None, source=None) -> dict`.
   - It validates: the kinds, 1..240 minutes for `extra_minutes`, and 1..20 unique ids.
   - It calls `cast.override` and returns the cast state.
   - `POST /admin/overrides` uses it too, so the HTML and JSON paths can't drift.
3. `build_admin_state(conn, config, cast_state, counts) -> dict`, following the shape in `docs/admin-api.md`.
   - It reads the profile names, avatars, allowances, modes and max sessions from the DB (`store.profile_policies` and the `profile` table), and the rest from the cast state.
   - `last_five` means `remaining_s` is not null and at most 300 (the kid app's `LAST_FIVE_S`).
   - With the cast service unreachable, it sets `tv.connection` to `"unreachable"` and `now_playing` to null, and keeps the last known profiles.
4. Count helpers:
   - `jobs.count_by_status(conn) -> dict[str, int]`;
   - `library.count_held_ready(conn) -> int` (held downloads that are ready to publish);
   - the disk figures from `library.disk_usage` and `shutil.disk_usage`, cached for 10 s.
5. Hub:
   - Generalise `KidHub` so it takes an optional `unreachable` callable (the default is the current kid one) and `refresh_s` (the default `None`; when set, it re-reduces and publishes the last cast state every `refresh_s` seconds).
   - Create `app.state.admin_hub` with `refresh_s=10`, started and stopped in the lifespan next to the kid hub.
   - The kid hub's behaviour must not change.
6. Routes, as in `docs/admin-api.md`:
   - `GET /api/info` needs no auth;
   - `GET /api/admin/state` and `GET /api/admin/events` need `read`: SSE, the first event immediately, a 15 s keepalive, reusing `hub.sse_stream`;
   - `POST /api/admin/overrides/{extra,unlimited,block,stop}` and `DELETE /api/admin/overrides/today` need `control`.
   - Overrides pass `source=token.name` and return the admin state built from the returned cast state.
   - The error codes are 422, and 503 for `CastUnavailable`.

**Tests:**
- auth: a missing, malformed, wrong or revoked token; read-only on the control routes;
- `/api/info` without auth;
- the state fields in absolute units, per profile;
- each override route makes the right `FakeCast.override` call, with `source` set to the token name;
- the HTML dashboard and the JSON API make identical cast calls;
- a 503 when the cast service is down;
- a 422 for 0 or 241 minutes and for an empty or duplicate `profile_ids`;
- SSE: the first event, and a keepalive with a short interval;
- the hub's `refresh_s`.

## Subagent C: admin UI and history

**Files:**
- `tellybox/web/admin/`: a new `integrations.py`, a template `integrations.html`, the nav in the base template, `PAGE_MODULES`, the history template and `history.py` in that folder;
- `tellybox/history.py`, `tellybox/locale/`;
- `tests/web/admin/`, `tests/test_history.py`.

Don't touch `dashboard.py`.

**Contract to read:** `tellybox/api_tokens.py`, migration `007_api.sql`, and `docs/admin-api.md` (the auth and scopes).

**Tasks**
1. `/admin/integrations`, with a nav entry "Integrations" after Settings.
   - A create form: the name (1–40 characters, required), and a scope radio, "Read only" or "Read and control" (the default).
   - After a create, show the secret **once**: in the redirect's response page, never in a URL or flash cookie. Render it directly (POST then 200), with a copy button and the text "Copy this token now; it won't be shown again."
   - A list: the name, the scopes, created, last used ("never"), and revoked (greyed out).
   - Revoke is a POST with a JS confirm.
   - Short help: the API is described in the project docs (`docs/admin-api.md`), and Home Assistant's own Cast entity bypasses the timer, so hide it from dashboards kids can reach.
   - Every POST goes through the existing `AdminGuard` and Origin check. Escape the names.
2. History: `history.history_days` includes `source` on override entries. The template shows "via %(name)s" after the override label when it is set.
3. i18n:
   - mark every new string;
   - add any admin JS strings to `js_strings.py`;
   - run `scripts/i18n.sh`;
   - translate the new entries into nl and de, informally ("je", "du").

   `tests/test_i18n.py` must pass.

**Tests:**
- create shows the secret once, and the DB holds only its hash;
- a second GET doesn't show it;
- the list, and revoke (after which `api_tokens.authenticate` returns None);
- an empty or too-long name is refused;
- a POST without Origin gets a 403;
- the history "via" label.

## Controller checklist after the subagents

1. Merge A, B and C into `step9/ha-api`, resolving conflicts. Run the full suite, `scripts/i18n.sh` and `tests/test_i18n.py`.
2. Review:
   - every `/api/admin` route has a `TokenGuard`;
   - no token in logs or responses after creation;
   - HTML and JSON share `apply_override`;
   - a revoked token gets a 401;
   - names are escaped;
   - the kid hub is unchanged.
3. Smoke test with uvicorn and a fake cast: create a token, `curl` the state and events, run an override, check a read-only token gets a 403, revoke and get a 401.
4. Update `docs/PROGRESS.md`, push, and open the PR, with the owner's device checks listed as open.
