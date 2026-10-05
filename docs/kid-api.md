# <picture><source media="(prefers-color-scheme: dark)" srcset="images/brand/mark-dark.svg"><img src="images/brand/mark.svg" alt="" width="36" align="top"></picture> Kid API contract (step 4; tile titles shown since step 7; profiles since step 8; reader UI since step 16)

The kid app (static files in `tellybox/web/static/`) talks only to these endpoints of the
`web` service. No login (NF-1). Only visible content ever appears: hidden shows/episodes and
held downloads are never listed, playable or served as images.

**Profiles (v2, step 8).** Each device picks who's watching (PR-2) and passes that group on
every call. A group is a comma-separated list of profile ids (`?profiles=1,3`) or a JSON list
(`"profile_ids": [1, 3]`), 1–20 ids, each an existing profile; anything else is a 400
`{"detail": "bad_profiles"}`. Omitting the group means the first profile in the admin's order
(the v1 behaviour with a single profile). Which shows a profile sees is set by the admin (PR-5, A-37), and a group
sees the intersection (PR-6); profiles also separate time, continue watching and progress.

## Types

```jsonc
// KidState: everything the kid screen shows live (KA-6..KA-9)
{
  "tv": "ok" | "unreachable",              // cast service down, or Chromecast not connected
  "device_name": "Living Room TV" | null,   // step 16 (KA-11): name of the TV playback is on (or would start on);
                                            // shown only by the reader UI; null when no Chromecast is known
  "now_playing": null | {
    "episode_id": 4, "show_id": 2,
    "thumb": "/img/episode/4.jpg",
    "title": "Alongside",                   // screen-reader label of the now-playing thumbnail; not shown
    "state": "loading" | "playing" | "paused" | "buffering"
  },
  "watching": [1, 3],                       // v2: profile ids of the current watchers; [] when nothing plays
  "sky": {
    "fraction_left": 0.62,                  // 0..1 of today's allowance (+extra) left; null when unlimited
    "last_five": false,                     // <= 5 min left (and not unlimited)
    "unlimited": false
  },
  "time_up": false,                         // picks disabled (KA-9); may be true while an episode finishes (grace)
                                            // sky and time_up describe the current watchers; a device reduces
                                            // "profiles" for its own group instead (min fraction, any time_up)
  "profiles": {                             // v2: every profile, keyed by id (JSON object keys are strings)
    "1": {"fraction_left": 0.62 | null, "last_five": false, "unlimited": false, "time_up": false}
  },
  "day": "2026-09-29",                      // v2: timer day; a device asks who's watching again when it changes
  "sessions": [                             // step 17 (PB-7, WT-12): every active playback, household-wide (no titles);
                                            // the app keeps the ones whose profile_ids are its own group. The TV session
                                            // is also described by now_playing/watching above, which stay as they were.
    {"key": "tv" | "device:<device_id>", "target": "tv" | "device",
     "label": "Living Room TV" | "iPhone Safari",    // the TV's name, or the browser label the server derived
     "device_id": null | "…", "episode_id": 4, "show_id": 2, "profile_ids": [1, 3],
     "state": "loading" | "playing" | "paused" | "buffering"}
  ]
}

// Profile (v2)
{ "profile_id": 1,
  "name": "Mila",                           // screen-reader label only; never shown (KA-2)
  "picture": "/img/profile/1.jpg" | null,   // uploaded photo; wins over avatar
  "avatar": "fox" | null,                   // built-in avatar: /static/avatars/{avatar}.svg
  "ui_mode": "icons" | "text",              // step 16 (KA-11): "icons" is the no-reading app (default); "text" the reader UI
  "watch_in_app": false,                    // step 17 (AD-7, KA-13): may play on the device itself; false hides the TV/phone toggle
  "time_up": false, "fraction_left": 0.62 | null, "last_five": false, "unlimited": false }

// Tile
{ "episode_id": 4, "show_id": 2, "thumb": "/img/episode/4.jpg",
  "title": "Alongside",                     // shown as a small caption under the thumbnail, max two lines
                                            // (KA-10), and the tile's screen-reader label; may be ""
  "progress": 0.42,                         // 0..1 watched, null if never started
  "finished": false }
```

**Show visibility (step 18, PR-5..PR-8, KA-15).** Listings only contain shows the whole group may
see (the intersection, PR-6); the continue-watching list is filtered the same way and resume
positions are kept, so a re-granted show resumes. When nothing is visible, `/api/kid/home` answers
200 with `{"continue": [], "shows": []}`: the app draws a picture-only empty state (a sleepy TV, no
text). That empty answer is distinct from loading or an error, which never reach the renderer.
A show or episode the group may not see is answered like a missing one: 404 on `/api/kid/shows/{id}`
and 404 `{"detail": "not_found"}` on
`/api/kid/play` and the device play call; a refused pick never changes what is playing. Revoking
access lets the episode in progress finish (its scoped media URL keeps working, PR-8), but autoplay
does not continue into the hidden show. Images (`/img/show|episode`) are not profile-scoped: they are 404 only for hidden or unpublished content, not for a show hidden from one profile.

## Endpoints

| Method | Path | Response |
|---|---|---|
| GET | `/` | the kid app shell (`static/index.html`) |
| GET | `/static/...` | static assets |
| GET | `/manifest.webmanifest` | web app manifest |
| GET | `/api/kid/profiles` | `[Profile]` in the admin's order (v2) |
| GET | `/api/kid/home?profiles=1,3` | `{"continue": [Tile & {"kind": "resume" \| "next"}], "shows": [{"show_id", "artwork": "/img/show/2.jpg", "title"}]}`; the show `title` is shown as a small caption (KA-10) and is the tile's screen-reader label. v2: `continue` merges the group's lists by recency without duplicates; tile `progress` is the most recent position among the group |
| GET | `/api/kid/shows/{id}?profiles=1,3` | `{"show_id", "artwork", "title", "episodes": [Tile]}` in episode order; 404 if hidden/missing |
| GET | `/api/kid/state` | KidState |
| GET | `/api/kid/events` | SSE, one `data: <KidState JSON>` per change, `: keepalive` comments every 15 s; first event immediately |
| POST | `/api/kid/play` `{"episode_id": 4, "profile_ids": [1, 3]}` | 200 KidState; 400 bad group; 404 `{"detail": "not_found"}` if not visible; 409 KidState when any member of the group is out of time (PR-4); 503 KidState when the TV is unreachable. Step 17 adds `"target": "tv" \| "device"` (default `"tv"`, the behaviour above) and `"device_id"`: see "Watching in the app" |
| POST | `/api/kid/device/heartbeat` `{"device_id", "state", "position_s", "duration_s"?}` | the cast service's answer unchanged: `{"action", "reason", "time_up", "grace_deadline", "next"?}`; see "Watching in the app". 422 invalid body; 503 `{"error": "unavailable"}` when the cast service is unreachable |
| POST | `/api/kid/device/stop` `{"device_id"}` | 200 KidState; ends this device's session; 503 KidState when unreachable |
| POST | `/api/kid/pause`, `/api/kid/resume` | 200 KidState; 503 KidState when unreachable |
| GET | `/img/profile/{id}.jpg` | the profile's uploaded photo (v2); 404 if none |
| GET | `/img/episode/{id}.jpg`, `/img/show/{id}.jpg` | image; 404 unless visible. Show artwork falls back to the first visible episode's thumbnail; if nothing is available, 404 (the frontend draws a placeholder). |

## Watching in the app (step 17, PB-7..PB-9, KA-13, KA-14)

`POST /api/kid/play` with `"target": "device"` and a `"device_id"` (a random id the browser keeps: letters, digits,
`-`, `_`; 8-64 characters; required for this target, else 422) plays the episode in the browser instead of on the TV.
It needs no Chromecast. Every profile of the group must have `watch_in_app` (AD-7), else 403 `{"error": "not_allowed"}`.
The browser label ("iPhone Safari", "Android Chrome", ...) is derived by the server from the User-Agent, never sent
by the app. Answers: 200 `{"url": "/media/4/<expires>/<sig>.mp4?s=<session>", "start_s": 0, "session": Session, "state": KidState}`
(`url` is relative to the page and works only while that session is open; `start_s` is the group's saved position);
400 bad group; 404 not visible; 409 KidState when the group may not start; 503 KidState when the cast service is unreachable.

While it plays the page sends `POST /api/kid/device/heartbeat` every 10 s with `state` (`playing`, `paused`, `buffering`,
`ended`, `error`), `position_s` and, when known, `duration_s`. The answer is `action` `continue`, `stop` or `next`
with a `reason` (`time_up`, `blocked`, `stop_now`, `replaced`, `disconnected`, `error`, `finished`, `unknown_session`),
`time_up` (the group has run out of time, finish the episode), `grace_deadline` and, for `next`, `next: {session, url, start_s}`
(autoplay, PB-3). On `stop` the page leaves the player. `POST /api/kid/device/stop` ends the session when the page leaves
it on its own. Details: `docs/cast-api.md` ("Device sessions").

## Reader UI (step 16, KA-11, PB-6)

`ui_mode` on each Profile tells the app which UI to draw. A device in group mode uses the reader UI only when
every selected profile has `"text"`; one `"icons"` profile makes the whole device use the icon UI. The server
doesn't enforce this; the data for both UIs is the same (titles are already in every Tile and in `now_playing`).
`device_name` is the TV the cast service is connected to. A pick for a profile with its own TV switches to it
(the cast service stops what plays on the old one first), so the name follows the last pick. The kid app
never chooses a TV; the admin sets each profile's default on the Settings page (PB-6).
