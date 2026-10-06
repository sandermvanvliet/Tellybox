# <picture><source media="(prefers-color-scheme: dark)" srcset="images/brand/mark-dark.svg"><img src="images/brand/mark.svg" alt="" width="36" align="top"></picture> Cast service API contract (internal; profiles since step 8, receiver since step 13, admin API since step 9)

The `cast` service listens on `127.0.0.1` only; the `web` service is its only client (`tellybox/web/cast_client.py`). Only the cast service writes timer, history and position tables. Fields marked **v2** are new in step 8 (kid profiles), **v7** in step 13 (Tellybox receiver, see `docs/receiver-protocol.md`), **v2.1** in step 9 (admin API, see `docs/admin-api.md`).

## State

`GET /state`, every `POST` response, and each `data:` event of `GET /events` (SSE, `: keepalive` every 15 s):

```jsonc
{
  "connection": "CONNECTED" | "CONNECTING" | "DISCONNECTED" | "FAILED" | ...,
  "device": null | {"uuid": "...", "name": "..."},   // the connected TV; changes when a pick uses a profile's own TV (PB-6)
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
        "watching": true                     // v2: one of the profiles of any session (sessions[].profile_ids);
                                             // false when nothing plays, even though the timer keeps the last group
      }
    ]
  },
  "sessions": [                              // step 17 (PB-8, WT-12): every active session, the TV first
    {
      "key": "tv" | "device:<device_id>", "target": "tv" | "device",
      "label": "Living Room TV" | "iPhone Safari",   // the TV's name, or the device's short browser label
      "device_id": null | "…",               // null for the TV
      "episode_id": 4, "show_id": 2, "title": "Alongside",
      "state": "loading" | "playing" | "paused" | "buffering",
      "position_s": 312, "duration_s": 420 | null,
      "profile_ids": [1, 3]
    }
  ],
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
| POST | `/play` | `{"episode_id": 4, "profile_ids": [1, 3]}` **v2**: 1–20 ids, required. **Step 16 (PB-6):** the target TV is the first listed profile's default TV (`profile.cast_device_uuid`), else the globally selected device. When it isn't the connected device, a pick that may start first stops what plays on the old one (watch session ended as `stopped`), connects to the new TV (up to 20 s), then loads there; the selected device in `/devices` is not changed. A refused pick (409) never switches. | 200 state. 404 no such episode. 409 `{"detail": {"error": "time_up", "reason": ...}}` when the group may not start (any member out of time, blocked or past its session max, PR-4). 422 empty, too long or unknown profile ids. 503 no Chromecast (none connected, and neither the profile nor the global setting names a known one), 502 command failed, or the profile's TV didn't connect within 20 s. |
| POST | `/pause`, `/resume`, `/stop` | none | 200 state; 503/502 as above |
| POST | `/overrides` | `{"kind": "extra_minutes" \| "unlimited" \| "block" \| "stop_now" \| "clear", "value": int \| null, "profile_ids": [int] \| null, "profile_id": int \| null, "source": str \| null}` | 200 state; 422 bad kind or value, unknown profile ids, or both `profile_ids` and `profile_id`. **v2.1:** `profile_ids` (1–20 ids) replaces `profile_id`, which is still accepted; neither = every profile. `clear` sets unlimited and blocked back to false (extra minutes stay), logging one `clear` row per profile. `source` (≤ 64 chars) is written to `override_log.source`: the API token's name, or null for the admin pages (HA-7). Blocking a profile that isn't watching doesn't stop playback. |
| GET | `/devices` | none | `{"selected": uuid \| null, "devices": [...]}` |
| POST | `/devices/select` | `{"uuid": "..."}` | 200 state; 404 unknown device |

## Device sessions (step 17: PB-7..PB-9, WT-10..WT-12)

A kid app can play an episode on the device itself instead of on the TV. These endpoints need no Chromecast. `now_playing`, `device`, `time_up` and `timer` keep describing the TV; device sessions appear in `sessions` only. A device is named by `device_id`, a random id the browser keeps (letters, digits, `-`, `_`; 8-64 characters). Each device session is its own timer playback, keyed `device:<device_id>` (PB-8).

| Method | Path | Body | Result |
|---|---|---|---|
| POST | `/device/play` | `{"device_id": "…", "label": "iPhone Safari", "episode_id": 4, "profile_ids": [1, 3]}`: `label` at most 40 characters, 1–20 profile ids. | 200 `{"session": Session, "url": "/media/4/<expires_at>/<sig>.mp4?s=<watch_session_id>", "start_s": 0}`. `start_s` is the group's saved position (PB-4). The URL is relative, signed over `episode:expires:session` and valid for 24 h, but the web app serves it only while that watch session is open (WT-11). 404 no such episode. 409 `{"detail": {"error": "time_up", "reason": ...}}` when the group may not start (nothing changes). 422 unknown profile, empty or too many ids, bad `device_id` or `label`. |
| POST | `/device/heartbeat` | `{"device_id": "…", "state": "playing" \| "paused" \| "buffering" \| "ended" \| "error", "position_s": 12.5, "duration_s": 420 \| null}` | 200 `{"action": "continue" \| "stop" \| "next", "reason": null \| "time_up" \| "blocked" \| "stop_now" \| "replaced" \| "disconnected" \| "error" \| "finished" \| "unknown_session", "time_up": false, "grace_deadline": null \| "ISO-8601 UTC", "next": {"session": Session, "url": "…", "start_s": 0}}` (`next` only with `action: "next"`). 422 invalid body. |
| POST | `/device/stop` | `{"device_id": "…"}` | 200 state. Ends the device's session as `stopped` and saves the position; an unknown device is a no-op. |

Behaviour:

- **Replacing (PB-8).** `/device/play` ends the device's own session as `replaced`, and the sessions of its profiles on other targets: a TV session stops the Chromecast like any stop, a device session just ends (a group's session ends together). Conversely, `/play` for a profile in a device session ends that session. The device finds out on its next heartbeat (`stop` / `replaced`).
- **Timing (WT-10).** Time counts from a `playing` or `buffering` heartbeat onwards, and a `paused` heartbeat is a pause. After 30 s without a heartbeat counting stops, at that moment; after 5 min the session ends as `disconnected`, with the position kept up to the last heartbeat. A heartbeat credits at most 30 s of played time.
- **Limits (WT-11).** When the group's allowance or maximum session length runs out while the episode plays, the answer is `continue` with `time_up: true` and the `grace_deadline`; once the grace is over, or at once for a block or stop now, the session ends and the answer is `stop` with `time_up`, `blocked` or `stop_now`. A heartbeat from a session that ended earlier is answered with the reason it ended (remembered for an hour), else `unknown_session`. The `stop_now` and `block` overrides act on device sessions too (WT-12).
- **`ended`** finishes the episode and, if autoplay is on for the show and allowed (PB-3, WT-4), answers `next` with a new session (same device and profiles); otherwise `stop` with `finished`, `time_up` or `blocked`. **`error`** ends the session as `load_failed` (PB-9).
- **Finished (PB-8).** An episode is marked finished only at 95% of its length with at least half of it actually played (accumulated playing time), however it ended; otherwise only the position is saved.
- **Restart (NF-7).** Open device sessions are closed as `restart` at start-up; only the TV session is re-attached. The history row (`watch_session`) records `target` (`tv` or `device`) and `device_label` (migration 016).

## Typed events (step 21, HA-13)

`GET /typed-events` (SSE, localhost like the rest of this API; `: keepalive` every 15 s) streams the cast service's discrete events as `data: <event dict>` frames, one per event, in the order they happened. The web service reads it and relays it on `GET /api/admin/events?typed=1` (see `admin-api.md`, "Typed events", for the catalog and the envelope). This stream is separate from `GET /events`, which carries state snapshots only and is unchanged.

- Cast-side types: `playback_started`, `playback_stopped`, `time_up`, `last_five`, `override_applied`. The web service adds `inbox_item_arrived` and `download_ready` itself.
- **No replay and no persistence.** A subscriber sees only events emitted while it is connected. Each subscriber has a bounded queue (100) that drops the oldest event when full, so a slow reader never blocks the controller.
- **Ordering.** An event is emitted after the state broadcast for the same cause.
- **Edges, not levels.** `time_up` and `last_five` fire once on the false to true edge of the watching group and re-arm when the value goes false (extra minutes, the daily reset), never once per tick.
- `override_applied.source` is the override's source verbatim (the API token's name, null for the admin pages).

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
