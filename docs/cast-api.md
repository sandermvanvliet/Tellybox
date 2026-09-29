# Cast service API contract (internal; profiles since step 8, receiver since step 13)

The `cast` service listens on `127.0.0.1` only; the `web` service is its only client (`tellybox/web/cast_client.py`). Only the cast service writes timer, history and position tables. Fields marked **v2** are new in step 8 (kid profiles), **v7** in step 13 (Tellybox receiver, see `docs/receiver-protocol.md`).

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
    "profiles": [                            // every profile, in id order
      {
        "profile_id": 1,
        "day": "2026-09-29",                 // timer day (local, relative to the reset time, WT-1)
        "used_s": 1500, "extra_s": 600,
        "unlimited": false, "blocked": false,
        "remaining_s": 1700 | null,          // v2: this profile alone; null = unlimited
        "can_start": true,                   // v2: could this profile start a pick on its own?
        "reason": null | "allowance" | "session_max" | "blocked",   // v2: why not
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
    "last_error": null | "launch timed out"  // why it failed, for the dashboard
  }
}
```

## Commands

| Method | Path | Body | Result |
|---|---|---|---|
| POST | `/play` | `{"episode_id": 4, "profile_ids": [1, 3]}` **v2**: 1–20 ids, required | 200 state. 404 no such episode. 409 `{"detail": {"error": "time_up", "reason": ...}}` when the group may not start (any member out of time, blocked or past its session max, PR-4). 422 empty, too long or unknown profile ids. 503 no Chromecast, 502 command failed. |
| POST | `/pause`, `/resume`, `/stop` | none | 200 state; 503/502 as above |
| POST | `/overrides` | `{"kind": "extra_minutes" \| "unlimited" \| "block" \| "stop_now", "value": int \| null, "profile_id": int \| null}` | 200 state; 422 bad value. `profile_id` null = every profile. Blocking a profile that isn't watching doesn't stop playback. |
| GET | `/devices` | none | `{"selected": uuid \| null, "devices": [...]}` |
| POST | `/devices/select` | `{"uuid": "..."}` | 200 state; 404 unknown device |

## Behaviour notes (v2)

- A pick makes its group the watchers, replacing what plays (A-5). Autoplay keeps the group; so does re-attaching after a restart.
- Only watchers accrue time. Each profile has its own viewing session and 15-minute break (WT-3, A-13); a profile that stops watching starts its break then.
- The resume position for a group is the most recently updated unfinished position among its members; the position is saved for every member (PB-4).
- A profile deleted while watching is dropped from the watchers and from the current episode's profiles on the next persist (at most 15 s).
