# Step 9: Admin API for Home Assistant (v2.1, HA-1..HA-8)

Status: approved by the owner on 2026-09-29. Branch: `step9/ha-api`.

This is Phase 0 of the owner's plan "Tellybox × Home Assistant: upstream features & integration plan". That plan describes a `local_push` Home Assistant integration that mirrors Tellybox's live state and exposes the parent controls, without ever becoming a way around the timer. The integration itself, `pytellybox` and `ha-tellybox`, lives in separate repositories and comes later. This step adds the Tellybox API it needs:
- **F1:** API tokens;
- **F2:** the admin state and its event stream;
- **F3:** the override endpoints;
- **F5 (part):** the instance id and `/api/info`.

## Decisions (owner, 2026-09-29)

- **Scope:** Phase 0 only. The F4 typed events, zeroconf, and the F6 settings, history and publish endpoints come later.
- **Build order:** this is inserted as step 9. SponsorBlock becomes step 10, subscriptions 11 and manual splitting 12. The Tellybox receiver keeps step 13, because it is already in progress, so smart splitting becomes 14. The release labels v3..v7 stay the same, and this step ships as v2.1.
- **Profiles:** every override takes an optional `profile_ids`, where none means everyone, like the dashboard's per-profile and "Everyone" buttons. The state carries `profiles[]` in absolute seconds.
- **Tokens:**
  - They are independent of the admin password: changing the password doesn't revoke them.
  - A token is shown once. Revoked tokens stay listed, greyed out.
  - There are two scopes, `read` and `control`; `control` implies `read`.
- **Extra minutes:** 1..240 per call, with no daily cap, because a control token carries the parent's authority.
- **The kid app** doesn't show where an override came from.

These become PRD assumptions A-16..A-18 in part 0.

## What already exists

- **Overrides:**
  - The admin dashboard posts `POST /admin/overrides` (`tellybox/web/admin/dashboard.py`).
  - That calls `CastClient.override(kind, value, profile_id)`.
  - The cast service's `POST /overrides` then calls `CastController.override`, which writes `override_log` through `store.log_override`, one row per profile.
  - There is no "clear today" operation, only `unlimited` or `block` with value 0.
- **Auth:** `AdminGuard` (`admin/common.py`) needs the session cookie (path `/admin`) and the Origin check, so a token API needs its own dependency. `auth.py` already stores only a SHA-256 of session tokens.
- **Settings and version:**
  - `settings` is a single row that later migrations extend with `ALTER TABLE`.
  - There is no instance id yet.
  - The version comes from `config.version`, which `/healthz` also returns.
- **Hubs and streams:**
  - `KidHub` (`tellybox/web/hub.py`) relays the cast service's stream once for every kid client, through a `reduce` callable.
  - The admin dashboard's SSE opens one upstream connection per client.
- **The cast `state()`** (`docs/cast-api.md`) has per-profile `used_s`, `extra_s`, `remaining_s`, `unlimited`, `blocked`, `can_start`, `reason` and `watching`, and group-level grace and session. It doesn't carry the next reset or a per-profile `session_elapsed_s`; `ProfileStatus` computes the latter, but `_profile_state` drops it.
- **Counts:** there are no count helpers for jobs or held downloads, only `jobs.list_jobs` and `library.list_held_downloads`.
- **Migration numbering:** step 13 (the receiver) added `006_receiver.sql`, so this step takes `007_api.sql`.

## Part 0: contract (controller, before the subagents)

1. **PRD:**
   - a section "Admin API (Home Assistant)" with HA-1..HA-8;
   - the release table and the build order renumbered;
   - A-16..A-18.
2. **`CLAUDE.md`:** the build order.
3. **Migration `007_api.sql`:**
   - `api_token(id, name, token_hash UNIQUE, scopes, created_at, last_used_at, revoked_at)`;
   - `settings.instance_id`, backfilled with 32 random hex characters;
   - `override_log.source TEXT`. NULL means the admin pages; otherwise it holds the token's name at the time.
4. **`tellybox/api_tokens.py`**, written in full with tests, because both B and C use it:
   - `create_token`, `list_tokens`, `revoke_token`, `authenticate` and `instance_id`;
   - secrets are `tbx_` plus 43 url-safe characters, and only the SHA-256 is stored;
   - `last_used_at` is written at most once a minute.
5. **`CastClient.override(kind, value=None, profile_ids=None, source=None)`**, the same signature on the web tests' `FakeCast`, and the dashboard's call updated to match.
6. **Docs:**
   - `docs/admin-api.md`, the full contract: auth, scopes, the state shape, endpoints and errors;
   - `docs/cast-api.md`: `/overrides` with `profile_ids`, `source` and the `clear` kind; the state's `timer.next_reset` and `profiles[].session_elapsed_s`.
7. **`docs/PROGRESS.md`:** step 9 in progress.

## Part A: cast service (subagent A)

- **`POST /overrides`** takes `profile_ids` (1–20 ids; `profile_id` is still accepted) and `source` (up to 64 characters).
  - The new kind `clear` sets unlimited and blocked back to false.
  - Unknown profiles are a 422.
- **`CastController.override(kind, value=None, profile_ids=None, source=None)`**: the override log gets `source`.
- **`state()`** adds `timer.next_reset` and `profiles[].session_elapsed_s`.

## Part B: JSON API and hub (subagent B)

- **`TokenGuard(conn, clock, scope)`** returns 401 with `WWW-Authenticate: Bearer`, or 403 for the wrong scope, and sets `request.state.api_token`.
- **`tellybox/web/overrides.py`:** `apply_override(cast, kind, value, profile_ids, source)` is the one code path for the admin dashboard and the JSON API.
- **Hub:**
  - generalise `KidHub` with an optional `unreachable` reducer and a `refresh_s` that re-reduces the last cast state;
  - a second instance, `app.state.admin_hub`, reduces with `build_admin_state`;
  - job and disk figures are cached for 10 s.
- **Count helpers:** `jobs.count_by_status` and `library.count_held_ready`.
- **Routes:** `GET /api/info`, `GET /api/admin/state`, `GET /api/admin/events`, and the override routes (see `docs/admin-api.md`).

## Part C: admin UI and history (subagent C)

- **`/admin/integrations`**, with a nav entry:
  - create a token (a name, and read-only or read + control), showing the secret once with a copy button;
  - a list with the created and last-used times;
  - revoke, with a confirm;
  - short help, including the warning that Home Assistant's own Cast entity bypasses the timer.
- **History:** override rows show "via <token name>" when `source` is set.
- **i18n:** nl and de, informal.

## Execution (at most 3 subagents)

- The controller writes and commits part 0 on `step9/ha-api`.
- A, B and C run in parallel on Sonnet, each in its own worktree off the contract commit. The briefs are in `docs/plans/step9-handoff.md`.
- The controller merges A, B, C in that order, runs the full suite and `scripts/i18n.sh`, then reviews:
  - every `/api/admin` route requires a token;
  - the token never appears in logs or responses after creation;
  - JSON and HTML overrides go through the same code path;
  - revoked tokens get a 401;
  - profile names are escaped on the Integrations and History pages.
- Then a manual smoke test with a fake cast, and the PR.

## Tests

- **`api_tokens`:**
  - a create returns the secret once and stores only its hash;
  - `authenticate` accepts a valid secret and refuses a wrong or revoked one;
  - the `last_used_at` throttle;
  - the scopes;
  - the instance id stays stable across calls.
- **Cast (A):**
  - an override for a subset of profiles;
  - `clear`;
  - `source` in the override log;
  - an unknown profile gives 422;
  - `next_reset` and `session_elapsed_s` in the state;
  - `profile_id` still works.
- **API (B):**
  - a missing, wrong, revoked or read-only token (401/403);
  - the state in absolute units, per profile;
  - each override reaches the cast service with the right kind, `profile_ids` and `source`;
  - the HTML and JSON paths make identical cast calls;
  - the SSE first event and keepalive;
  - a 503 when the cast service is down;
  - `/api/info` without a token.
- **Admin (C):**
  - the Integrations page: create, list and revoke, with the Origin check;
  - the history "via" label;
  - i18n complete.
- The existing tests keep passing.

## Real-device checks (owner, after deploy)

1. On `/admin/integrations`, create a token "Home Assistant" with control, and a read-only token "Hallway tablet".
2. `curl -H "Authorization: Bearer …" https://<tellybox>/api/admin/state` shows the live state. `curl -N …/api/admin/events` streams updates while an episode plays.
3. `POST /api/admin/overrides/extra {"minutes": 15}` moves the sun up on the kid's phone. The same call with the read-only token gets a 403.
4. `POST /api/admin/overrides/block` for the watching profile stops the TV immediately. `DELETE /api/admin/overrides/today` lifts the block.
5. History shows the overrides "via Home Assistant".
6. Revoke the token: the next call gets a 401.
