# <picture><source media="(prefers-color-scheme: dark)" srcset="images/brand/mark-dark.svg"><img src="images/brand/mark.svg" alt="" width="36" align="top"></picture> Cast service API contract (internal; profiles since step 8, receiver since step 13, admin API since step 9)

The `cast` service listens on `127.0.0.1` only; the `web` service is its only client (`tellybox/web/cast_client.py`). Only the cast service writes timer, history and position tables. Fields marked **v2** are new in step 8 (kid profiles), **v7** in step 13 (Tellybox receiver, see `docs/receiver-protocol.md`), **v2.1** in step 9 (admin API, see `docs/admin-api.md`).

## State

`GET /state`, every `POST` response, and each `data:` event of `GET /events` (SSE, `: keepalive` every 15 s):

```jsonc
{
  "connection": "CONNECTED" | "CONNECTING" | "DISCONNECTED" | "FAILED" | ...,
  "device": null | {"uuid": "...", "name": "..."},
  "now_playing": null | {
    "episode_id": 4, "show_id": 2, "title": "Alongside",
    "state": "loading" | "playing" | "paused" | "buffering",
    "position_s": 312, "duration_s": 420,
    "profile_ids": [1, 3]                    // v2: the watchers of this episode, sorted
  },
  "timer": {
    // The top-level fields describe the current watchers (the last pick's group).
    // With no watchers yet today they describe every profile, as in v1.
    "remaining_s": 1234 | null,              // min across watchers; null = all unlimited
    "can_start": true,                       // may the watchers pick again now?
    "action": "continue" | "finish_then_stop" | "stop_now",
    "reason": null | "allowance" | "session_max" | "blocked",
    "grace_deadline": null | "ISO-8601 UTC",
    "session_started_at": null | "ISO-8601 UTC",   // earliest open session among watchers
    "session_elapsed_s": null | 1800,
    "next_reset": "ISO-8601 UTC",            // v2.1: the next daily reset (WT-1)
    "profiles": [                            // every profile, in id order
      {
        "profile_id": 1,
        "day": "2026-09-29",                 // timer day (local, relative to the reset time, WT-1)
        "used_s": 1500, "extra_s": 600,
        "unlimited": false, "blocked": false,
        "remaining_s": 1700 | null,          // v2: this profile alone; null = unlimited
        "can_start": true,                   // v2: could this profile start a pick on its own?
        "reason": null | "allowance" | "session_max" | "blocked",   // v2: why not
        "session_elapsed_s": null | 1800,    // v2.1: this profile's open viewing session (WT-3, A-13); null = none
        "watching": true                     // v2: one of the current episode's profiles (now_playing.profile_ids);
                                             // false when nothing plays, even though the timer keeps the last group
      }
    ]
  },
  "time_up": false,                          // = not timer.can_start (the watchers' KA-9)
  "receiver": {                              // v7 (CR-6)
    "kind": "tellybox" | "default",          // what the next pick will use (or the current one uses)
    "configured": true,                      // settings.receiver_app_id is set
    "fallback_until": null | "ISO-8601 UTC", // Tellybox receiver failed; Default Media Receiver until then
    "last_error": null | "launch timed out", // why it failed, for the dashboard (in memory: gone after a restart)
    "failures_24h": 0,                       // receiver resilience (CR-6): problems in the receiver event log, last 24 h
    "launches_24h": 12,                      //   successful launches or loads into our receiver, last 24 h
    "last_failure": null | {                 //   the latest problem, however old (the log keeps 21 days)
      "kind": "launch_failed" | "refused" | "lost" | "recover_failed" | "page_error",
      "detail": "attempt 2: launch timed out" | null,
      "at": "ISO-8601 UTC"
    },
    "refused": false                         // the last failure was the Chromecast refusing the app, and the fallback is active
  }
}
```

## Commands

| Method | Path | Body | Result |
|---|---|---|---|
| POST | `/play` | `{"episode_id": 4, "profile_ids": [1, 3]}` **v2**: 1–20 ids, required | 200 state. 404 no such episode. 409 `{"detail": {"error": "time_up", "reason": ...}}` when the group may not start (any member out of time, blocked or past its session max, PR-4). 422 empty, too long or unknown profile ids. 503 no Chromecast, 502 command failed. |
| POST | `/pause`, `/resume`, `/stop` | none | 200 state; 503/502 as above |
| POST | `/overrides` | `{"kind": "extra_minutes" \| "unlimited" \| "block" \| "stop_now" \| "clear", "value": int \| null, "profile_ids": [int] \| null, "profile_id": int \| null, "source": str \| null}` | 200 state; 422 bad kind or value, unknown profile ids, or both `profile_ids` and `profile_id`. **v2.1:** `profile_ids` (1–20 ids) replaces `profile_id`, which is still accepted; neither = every profile. `clear` sets unlimited and blocked back to false (extra minutes stay), logging one `clear` row per profile. `source` (≤ 64 chars) is written to `override_log.source`: the API token's name, or null for the admin pages (HA-7). Blocking a profile that isn't watching doesn't stop playback. |
| GET | `/devices` | none | `{"selected": uuid \| null, "devices": [...]}` |
| POST | `/devices/select` | `{"uuid": "..."}` | 200 state; 404 unknown device |

## Receiver resilience (CR-6)

The new `receiver` fields are additive. The cast service writes the `receiver_event` table (migration 011; the worker purges rows older than 21 days), and `receiver` summarises it.

- **Launch retry.** A pick that needs our receiver tries it up to twice: 8 s, then (after quitting a half-started app and waiting 1 s) 15 s. A refusal (the device rejects the app at once, or the launch error is `CANCELLED`) is not retried.
- **Fallback with backoff.** When both attempts fail, the pick plays on the Default Media Receiver. Consecutive failed picks set the fallback: the 1st none (the next pick or autoplay episode tries our receiver again), the 2nd 5 min, the 3rd 15 min, the 4th and later 30 min. A refusal sets 30 min at once. Any successful launch resets the count. `kind` is `default` while the current episode plays on the fallback, also without a `fallback_until`.
- **Mid-episode recovery.** When our receiver app vanishes during an episode (the device shows no app or the Backdrop, and no other app follows within 10 s, since a 1st-gen cold launch of another app takes seconds), the episode is relaunched once, at the estimated position, in the same watch session. Time isn't counted while the TV shows nothing. It isn't done when time is up or the profile is blocked, after our own stop, during the night hold, after a reconnect (PB-5), or a second time in the same episode: those end the episode as before. Another app taking the device is still a take-over (WT-9).
- **Event kinds** (`receiver_event.kind`): `launch_ok`, `launch_failed`, `refused`, `fallback`, `lost`, `recovered`, `recover_failed`, `page_error`. `launch_failed`, `refused`, `lost`, `recover_failed` and `page_error` count as failures.
- The admin API's `/api/admin/state` doesn't carry the `receiver` block.

## Behaviour notes (v2)

- A pick makes its group the watchers, replacing what plays (A-5). Autoplay keeps the group; so does re-attaching after a restart.
- Only watchers accrue time. Each profile has its own viewing session and 15-minute break (WT-3, A-13); a profile that stops watching starts its break then.
- The resume position for a group is the most recently updated unfinished position among its members; the position is saved for every member (PB-4).
- A profile deleted while watching is dropped from the watchers and from the current episode's profiles on the next persist (at most 15 s).
