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
  "sessions": [                          // step 17 (PB-7, WT-12): every active playback, the TV first
    {"key": "tv" | "device:<id>", "target": "tv" | "device",
     "label": "Living Room TV" | "iPhone Safari",   // the TV's name, or the browser label the server derived
     "device_id": null | "…", "episode_id": 4, "show_id": 2, "title": "…",
     "state": "loading" | "playing" | "paused" | "buffering",
     "position_s": 312, "duration_s": 660 | null, "profile_ids": [1, 3]}
  ],
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
      "picture": "/img/profile/1.jpg" | null,  // the uploaded photo (HA-11); null when there is none. avatar stays
      "watch_in_app": false,             // may watch in the browser or app (AD-7, HA-11). Read-only
      "ui_mode": "icons" | "text",       // the kid app style (KA-11, HA-11). Read-only
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
      "watching": true,                  // in the current episode, on the TV or in the app (step 17)
      "last_five": false,
      "visible_shows": 12                // shows this profile may see (PR-5, HA-10); 0 means an empty kid app. Read-only
    }
  ],
  "jobs": {"queued": 1, "running": 1, "failed": 0,  // running = downloading + processing
           "held_ready": 3},                        // held downloads ready to publish (waiting for approval)
  "disk": {"media_bytes": 48213000000, "free_bytes": 120000000000},
  "inbox": {"pending": 4,                // HA-9: subscription uploads waiting for approval (paused subscriptions included)
            "unhealthy": 0,              // subscriptions failing for 7 days or more (CS-7)
            "latest_received_at": "ISO-8601 UTC" | null}  // when the newest inbox item arrived; changes on every new upload
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
- **Refreshing:** `jobs` and `disk` are refreshed at most every 10 s. `inbox` is read from the database by the web service and pushed when it changes.
- **Profile fields (HA-11):** `picture`, `watch_in_app` and `ui_mode` are read-only and come straight from the database, so they are in every shape (cold start and an unreachable cast service included) and outside HA-8. There are no endpoints to change them. They are additive: `api` stays 1 and `/api/info` capabilities don't change. `picture` is a path on the Tellybox address; the photo, like the built-in avatar at `/static/avatars/<avatar>.svg`, is served without login as in the kid app (NF-1), so an integration fetches it itself. A settings change shows up in the state and the event stream at the next refresh (up to 10 s).
- **The inbox is read-only and independent of the cast service (HA-9):** it is filled from the database, so it stays accurate when `tv.connection` is `"unreachable"`. There is no way to approve or reject through this API, and HA-8 (playback and overrides go through the cast service) doesn't apply to it.

## Endpoints

| Method | Path | Scope | Body | Response |
|---|---|---|---|---|
| GET | `/api/info` | none | | `{"instance_id", "version", "api": 1, "capabilities": ["state", "events", "overrides", "profiles", "inbox"]}` (HA-6). Lets an integration identify the instance and check what it supports. |
| GET | `/api/admin/state` | read | | AdminState (HA-2) |
| GET | `/api/admin/events` | read | | SSE (HA-3). The current AdminState right away, then one `data:` event per change, and `: keepalive` every 15 s. During playback, expect about one event per second (the position and time left). |
| POST | `/api/admin/overrides/extra` | control | `{"minutes": 15, "profile_ids": [1]?}` | AdminState. `minutes` is 1..240 (A-17) and adds to the day's extra time (WT-7). |
| POST | `/api/admin/overrides/unlimited` | control | `{"profile_ids": [1]?}` | AdminState. Unlimited today: lifts the allowance and the session max. |
| POST | `/api/admin/overrides/block` | control | `{"profile_ids": [1]?}` | AdminState. Takes effect immediately, without grace; playback stops if a blocked profile is watching. |
| POST | `/api/admin/overrides/stop` | control | `{}` | AdminState. Stop now: stops what's playing, without grace. |
| DELETE | `/api/admin/overrides/today` | control | query `?profile_ids=1,3` (optional) | AdminState. Clears unlimited and block for today; extra minutes stay (HA-5). |
| GET | `/api/admin/history` | read | query `?days=7&profile_ids=1,3` (both optional) | UsageHistory (HA-12). Daily totals and the last watched episode per profile, from the database; works while the cast service is down. Advertised by the `history` capability. |

- **`profile_ids`:** 1–20 distinct existing profile ids. Leave it out (or null) for every profile.
- **Errors:**
  - `422 {"detail": "…"}` for a bad body, minutes out of range, or bad or unknown `profile_ids`;
  - `503 {"detail": "cast_unavailable"}` when the cast service can't be reached, in which case nothing was applied.
- **History:** each override is recorded with the token's name as its source, and the admin History page shows it as "via <name>" (HA-7).

## History (HA-12, A-38)

`GET /api/admin/history` (scope `read`) gives, per profile, the daily totals of the last `days` timer days and the most recent episode that profile watched, so an integration can show yesterday's time and a weekly average. It is read-only and comes straight from the database, so it answers while the cast service is down and is outside HA-8. `/api/info` lists the `history` capability; an older Tellybox answers 404.

- **Query:** `days` is 1..21 (default 7), the number of timer days ending with today (the retention window of AD-5). `profile_ids` is optional, comma separated; an unknown id, or a `days` that is not an integer in range, gives `422 {"detail": "…"}`.
- **Timer days:** days follow the reset time (WT-1, default 04:00), never the calendar day. `today` in the response is the current timer day; a day without a row counts as zeros.

```jsonc
// UsageHistory: GET /api/admin/history
{
  "today": "2026-10-06",                 // the timer day at the time of the request
  "days": 7,
  "profiles": [                          // the admin's order (sort_order, id)
    {
      "id": 1, "name": "Mila",
      "days": [                          // exactly `days` entries, newest first, today first
        {"date": "2026-10-06", "used_s": 1200, "extra_s": 0, "unlimited": false, "blocked": false},
        {"date": "2026-10-05", "used_s": 2710, "extra_s": 900, "unlimited": false, "blocked": false}
      ],
      "last_watched": null | {           // her latest watch session (open or closed) inside the retention window
        "episode_id": 4 | null,          // null once the episode is deleted
        "title": "…" | null, "show": "…" | null,
        "started_at": "ISO-8601 UTC", "ended_at": "ISO-8601 UTC" | null,   // null = watching now
        "target": "tv" | "device"
      }
    }
  ]
}
```

- `used_s` is `daily_usage.seconds_used` rounded to whole seconds and `extra_s` is `extra_min` times 60. `daily_usage`, not `watch_session`, is the timer's truth: a session shared by two kids would be counted twice.
- `last_watched` carries the episode's title and show. Any `read` token sees them (A-38), the same trust as `now_playing` in the state.
- Nothing in this endpoint writes, plays or changes anything.

## Typed events (HA-13)

`GET /api/admin/events?typed=1` (scope `read`) also delivers typed events, so an integration doesn't have to diff states. Events carry what a diff cannot know: why playback stopped, and who applied an override. They are advisory edges, not stored and not replayed: events that happen while a client is disconnected are lost, and the state (the first `data:` frame on every connect) stays the source of truth. `/api/info` lists the `typed_events` capability; an older Tellybox ignores `typed=1` and sends only states.

- **Opt-in.** Without `typed=1` the stream is byte-identical to before (state frames only), so older clients keep working. With it the state frames still arrive exactly as before (default `message` event, `data: <AdminState>`), and typed events arrive as named SSE frames:

  ```
  event: playback_stopped
  data: {"type":"playback_stopped","at":"2026-10-06T18:02:11.000+00:00","profile_ids":[1],...}
  ```

- **Ordering.** For one cause the cast service emits the event after the state that reflects it, and the stream sends an event after a state that is already waiting, so in practice a consumer handling an event sees state at least as new as the event. This is best effort, not a guarantee: states and events travel on separate internal streams, so a client must not depend on strict ordering (an automation that needs the level should read the state, not assume it). Events of different types may interleave with states in any order.
- **Envelope.** Every event is `{"type": str, "at": ISO-8601 UTC string, ...fields}`; `type` equals the SSE `event:` name. Clients must ignore unknown fields and unknown types.
- **Slow clients.** Each connection has a bounded queue (100) that drops the oldest event when full. Nothing here blocks other clients or the timer.
- **Read-only.** The stream starts nothing and changes nothing (HA-8).

| `type` | Fields | Fires when |
|---|---|---|
| `playback_started` | `profile_ids`, `episode_id`, `show_id`, `title`, `show`, `target` (`tv` or `device`), `label` (the TV's name or the browser label) | a TV or in-app session starts (autoplay-next included) |
| `playback_stopped` | the same fields as `playback_started`, plus `reason` and `position_s` | a session ends. `reason` is one of `finished`, `replaced`, `stopped`, `parent_stop`, `time_up`, `blocked`, `taken_over`, `disconnected`, `restart`, `load_failed` |
| `time_up` | `profile_ids` (the watching group), `reason` (`allowance`, `session_max` or `blocked`) | the group's `time_up` goes false to true. It re-arms when it goes false again (extra time, the daily reset) |
| `last_five` | `profile_ids`, `remaining_s` | the group's `last_five` goes false to true; re-arms like `time_up` |
| `override_applied` | `kind` (`extra_minutes`, `unlimited`, `block`, `stop_now` or `clear`), `value` (minutes for `extra_minutes`, else null), `profile_ids` (resolved: every profile when "everyone"), `source` (the token's name, or null for the admin pages) | an override was applied |
| `inbox_item_arrived` | `pending`, `new_items` (at least 1), `latest_received_at` | `inbox.latest_received_at` advances |
| `download_ready` | `held_ready`, `new_ready` (at least 1) | the number of held downloads waiting for approval rises |

- `playback_started` and `playback_stopped` come from the cast service. For an autoplay-next, a `playback_stopped` with `reason` `finished` is followed by a `playback_started`. A pick that replaces a playing episode gives `playback_stopped` with `reason` `replaced`, then `playback_started`.
- `inbox_item_arrived` and `download_ready` come from the web service watching the database (they don't depend on the cast service), so they keep flowing while the TV is unreachable. The timer and playback events pause while the cast service is down.

## Safety notes for integrations (HA-8)

- Never cast to the Chromecast directly. Tellybox times and controls only playback it started, so Home Assistant's own Cast media player is an unmetered path: hide it from dashboards kids can reach.
- Put the override buttons on an admin-only dashboard.
- Give each integration its own token. Revoking a token stops that integration at once.
