# <picture><source media="(prefers-color-scheme: dark)" srcset="images/brand/mark-dark.svg"><img src="images/brand/mark.svg" alt="" width="36" align="top"></picture> Kid API contract (step 4; tile titles shown since step 7; profiles since step 8; reader UI since step 16)

The kid app (static files in `tellybox/web/static/`) talks only to these endpoints of the
`web` service. No login (NF-1). Only visible content ever appears: hidden shows/episodes and
held downloads are never listed, playable or served as images.

**Profiles (v2, step 8).** Each device picks who's watching (PR-2) and passes that group on
every call. A group is a comma-separated list of profile ids (`?profiles=1,3`) or a JSON list
(`"profile_ids": [1, 3]`), 1–20 ids, each an existing profile; anything else is a 400
`{"detail": "bad_profiles"}`. Omitting the group means the first profile in the admin's order
(the v1 behaviour with a single profile). Every profile sees the whole library (A-8); profiles separate
time, continue watching and progress only.

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
  "day": "2026-09-29"                       // v2: timer day; a device asks who's watching again when it changes
}

// Profile (v2)
{ "profile_id": 1,
  "name": "Mila",                           // screen-reader label only; never shown (KA-2)
  "picture": "/img/profile/1.jpg" | null,   // uploaded photo; wins over avatar
  "avatar": "fox" | null,                   // built-in avatar: /static/avatars/{avatar}.svg
  "ui_mode": "icons" | "text",              // step 16 (KA-11): "icons" is the no-reading app (default); "text" the reader UI
  "time_up": false, "fraction_left": 0.62 | null, "last_five": false, "unlimited": false }

// Tile
{ "episode_id": 4, "show_id": 2, "thumb": "/img/episode/4.jpg",
  "title": "Alongside",                     // shown as a small caption under the thumbnail, max two lines
                                            // (KA-10), and the tile's screen-reader label; may be ""
  "progress": 0.42,                         // 0..1 watched, null if never started
  "finished": false }
```

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
| POST | `/api/kid/play` `{"episode_id": 4, "profile_ids": [1, 3]}` | 200 KidState; 400 bad group; 404 `{"detail": "not_found"}` if not visible; 409 KidState when any member of the group is out of time (PR-4); 503 KidState when the TV is unreachable |
| POST | `/api/kid/pause`, `/api/kid/resume` | 200 KidState; 503 KidState when unreachable |
| GET | `/img/profile/{id}.jpg` | the profile's uploaded photo (v2); 404 if none |
| GET | `/img/episode/{id}.jpg`, `/img/show/{id}.jpg` | image; 404 unless visible. Show artwork falls back to the first visible episode's thumbnail; if nothing is available, 404 (the frontend draws a placeholder). |

## Reader UI (step 16, KA-11, PB-6)

`ui_mode` on each Profile tells the app which UI to draw. A device in group mode uses the reader UI only when
every selected profile has `"text"`; one `"icons"` profile makes the whole device use the icon UI. The server
doesn't enforce this; the data for both UIs is the same (titles are already in every Tile and in `now_playing`).
`device_name` is the TV the cast service is connected to. A pick for a profile with its own TV switches to it
(the cast service stops what plays on the old one first), so the name follows the last pick. The kid app
never chooses a TV; the admin sets each profile's default on the Settings page (PB-6).
