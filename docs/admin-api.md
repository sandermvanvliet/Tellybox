# <picture><source media="(prefers-color-scheme: dark)" srcset="images/brand/mark-dark.svg"><img src="images/brand/mark.svg" alt="" width="36" align="top"></picture> Admin API contract (step 9, v2.1, HA-1..HA-8)

A small JSON API on the `web` service for Home Assistant and similar home-automation tools. It mirrors the admin dashboard's live state and exposes the parent overrides. It never becomes a way around the timer (HA-8):
- every action goes through the cast service, exactly like the admin dashboard's buttons;
- playback control stays with the kid API (`docs/kid-api.md`), so a time-up 409 still applies.

Like everything else, it is for the LAN and Tailscale only (NF-4). Put it behind the same HTTPS reverse proxy as the admin pages.

## Auth (HA-1)

- **Tokens:** every `/api/admin/*` request carries `Authorization: Bearer <token>`.
  - Tokens are created and revoked on the admin page **Integrations** (`/admin/integrations`).
  - A token looks like `tbx_` followed by 43 url-safe characters. It is shown once; only its SHA-256 is stored.
  - Tokens are independent of the admin password: changing the password doesn't revoke them (A-16).
- **Scopes:**
  - `read`: the state and the events.
  - `control`: the overrides. It implies `read`.
  - Use a read-only token for wall tablets and status displays.
- **No cookies, no Origin check.** A browser can't send the header on its own, so there's no CSRF surface. The admin session cookie is never accepted here.
- **Errors:**
  - `401 {"detail": "unauthorized"}` with `WWW-Authenticate: Bearer`: the token is missing, malformed, unknown or revoked.
  - `403 {"detail": "forbidden"}`: the token lacks the scope.
- **Logging:** the token never appears in logs or responses after creation.

## Types

```jsonc
// AdminState: GET /api/admin/state, each /api/admin/events event, and every override response
{
  "instance_id": "3f2a…",                // HA-6: stable per installation (32 hex chars)
  "version": "2026.09.29.3",             // config.version ("dev" locally)
  "api": 1,
  "day": {"date": "2026-09-29" | null,   // the timer day (WT-1), the first profile's
          "resets_at": "ISO-8601 UTC" | null},  // the next daily reset (cast timer.next_reset)
  "tv": {
    "connection": "CONNECTED" | … | "unreachable",   // the cast state's connection; "unreachable" = cast service down
    "reachable": true,                   // connection == "CONNECTED"
    "device": null | "TV"
  },
  "now_playing": null | {
    "episode_id": 4, "show_id": 2, "title": "…", "show": "…",
    "state": "loading" | "playing" | "paused" | "buffering",
    "position_s": 312, "duration_s": 660,
    "profile_ids": [1, 3]                // who's watching this episode
  },
  "group": {                             // the current watchers, as the cast state's timer describes them
    "remaining_s": 1234 | null,          // null = all unlimited
    "time_up": false,                    // the watchers may not pick again (KA-9)
    "last_five": false,                  // remaining_s is not null and ≤ 300
    "action": "continue" | "finish_then_stop" | "stop_now",
    "reason": null | "allowance" | "session_max" | "blocked",
    "grace_ends_at": null | "ISO-8601 UTC",
    "session_started_at": null | "ISO-8601 UTC",
    "session_elapsed_s": null | 1800
  },
  "profiles": [                          // every profile, in the admin's order (sort_order, id)
    {
      "id": 1, "name": "Mila", "avatar": "fox" | null,
      "allowance_s": 3600 | null,        // the daily allowance (AD-2, A-23); null = unlimited
      "allowance_source": "inherit" | "custom" | "unlimited",  // A-23
      "extra_s": 900,                    // extra time given today
      "used_s": 2710,
      "remaining_s": 1790 | null,        // null = unlimited today
      "unlimited": false, "blocked": false,
      "mode": "ignore_pauses" | "wall_clock",
      "max_session_s": 5400 | null,      // WT-3; null = none (A-23)
      "max_session_source": "inherit" | "custom" | "unlimited",  // A-23
      "session_elapsed_s": 1200 | null,  // this profile's open viewing session (A-13)
      "can_start": true,
      "reason": null | "allowance" | "session_max" | "blocked",
      "watching": true,                  // in the current episode
      "last_five": false
    }
  ],
  "jobs": {"queued": 1, "running": 1, "failed": 0,  // running = downloading + processing
           "held_ready": 3},                        // held downloads ready to publish (waiting for approval)
  "disk": {"media_bytes": 48213000000, "free_bytes": 120000000000}
}
```

- **The cast service is unreachable:**
  - `tv.connection` is `"unreachable"`, `tv.reachable` is false and `now_playing` is null;
  - `group` and `profiles` keep their last known values, with `watching` false;
  - `GET /api/admin/state` still answers 200. Only the overrides return 503.
- **Cold start**, before the web service has seen any cast state:
  - `profiles` comes from the DB, with `used_s`, `extra_s`, `remaining_s`, `can_start` and `reason` null, and `unlimited`, `blocked` and `watching` false;
  - `day.date` and `day.resets_at` are null.
  - The same holds for a profile the cast state doesn't list yet, such as one just added.
- **Refreshing:** `jobs` and `disk` are refreshed at most every 10 s.

## Endpoints

| Method | Path | Scope | Body | Response |
|---|---|---|---|---|
| GET | `/api/info` | none | | `{"instance_id", "version", "api": 1, "capabilities": ["state", "events", "overrides", "profiles"]}` (HA-6). Lets an integration identify the instance and check what it supports. |
| GET | `/api/admin/state` | read | | AdminState (HA-2) |
| GET | `/api/admin/events` | read | | SSE (HA-3). The current AdminState right away, then one `data:` event per change, and `: keepalive` every 15 s. During playback, expect about one event per second (the position and time left). |
| POST | `/api/admin/overrides/extra` | control | `{"minutes": 15, "profile_ids": [1]?}` | AdminState. `minutes` is 1..240 (A-17) and adds to the day's extra time (WT-7). |
| POST | `/api/admin/overrides/unlimited` | control | `{"profile_ids": [1]?}` | AdminState. Unlimited today: lifts the allowance and the session max. |
| POST | `/api/admin/overrides/block` | control | `{"profile_ids": [1]?}` | AdminState. Takes effect immediately, without grace; playback stops if a blocked profile is watching. |
| POST | `/api/admin/overrides/stop` | control | `{}` | AdminState. Stop now: stops what's playing, without grace. |
| DELETE | `/api/admin/overrides/today` | control | query `?profile_ids=1,3` (optional) | AdminState. Clears unlimited and block for today; extra minutes stay (HA-5). |

- **`profile_ids`:** 1–20 distinct existing profile ids. Leave it out (or null) for every profile.
- **Errors:**
  - `422 {"detail": "…"}` for a bad body, minutes out of range, or bad or unknown `profile_ids`;
  - `503 {"detail": "cast_unavailable"}` when the cast service can't be reached, in which case nothing was applied.
- **History:** each override is recorded with the token's name as its source, and the admin History page shows it as "via <name>" (HA-7).

## Safety notes for integrations (HA-8)

- Never cast to the Chromecast directly. Tellybox times and controls only playback it started, so Home Assistant's own Cast media player is an unmetered path: hide it from dashboards kids can reach.
- Put the override buttons on an admin-only dashboard.
- Give each integration its own token. Revoking a token stops that integration at once.
