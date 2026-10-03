# Steps 17 and 18: seeking and watching in the app (issues #32 and #31)

Proposed 2026-10-03, awaiting owner approval. Nothing here is built yet, and the PRD and CLAUDE.md are untouched: the wording below ships with the implementation PRs, as step 15's plan did.

## Context

- Issue #32, "Allow for seeking in a playing video": a kid (or parent) can move backwards and forwards in the video that is playing.
- Issue #31, "Support watching in the app instead of casting": an in-app player, locked down per profile (default on), with a stretch goal of handing a video over between the app and the TV.
- Today the kid app has one now-playing bar with a thumbnail and a pause/resume button (`renderBar()` and `toggle()` in `tellybox/web/static/app.js`). KA-6 says "No seek, volume or skip controls". The hard rule in CLAUDE.md and PB-2 say episodes are always cast.
- Seeking is the smaller change and the in-app player builds on it (the same progress and position plumbing), so seek is step 17 and in-app playback is step 18.

Facts checked while planning:
- `docs/spike-casting.md` (spike row 6) shows seek works on the real Chromecast with the Default Media Receiver: `seek 60` gives BUFFERING for about 3 s, then PLAYING at 60.33, with Range requests (206).
- The cast service's `state()` (`tellybox/cast/controller.py`) already puts `position_s` and `duration_s` in `now_playing`. The kid app's `kid_state()` (`tellybox/web/kid.py`, the `now_playing` dict) drops them, so the kid API needs to pass them on.
- `PlayRequest`/`play` take no start position over the API today, but the device `play(...)` already has `start_s`, and `_start_episode` uses the saved position (PB-4).
- Media URLs (`tellybox/media_urls.py`) are HMAC-signed with `MEDIA_TTL_S = 24 * 3600`, so a copied URL works for 24 hours.
- The kid SSE stream (`tellybox/web/hub.py`, `sse_stream`) sends one `data: <KidState>` per change and has no named events.
- `Current` (`tellybox/cast/controller.py`) has no source field yet; the in-app session needs one.
- Migrations: `main` ends at `012_admin_setup.sql`. Step 16 (reader UI, `013_profile_ui_tv.sql`, KA-11, KA-12, PB-6) and the per-profile limits branch (`014_profile_limits.sql`, A-23) are not merged into `main` yet. This plan assumes both are merged first and uses 015. If they land in another order, renumber the migration and the requirement IDs when building.

Not confirmed from the repo (verify during the builds):
- Whether the Tellybox receiver (CAF, `tellybox/web/receiver/cast.js`) accepts SEEK from the sender. `supportedMediaCommands` is not set anywhere under `tellybox/web/receiver`, so the CAF default applies; it must be checked on the real device.
- How iOS Safari and Android Chrome behave for a fullscreen `<video>` with the screen locked or the tab in the background. This is a device check.

## Step 17: seeking (issue #32)

### Cast service
- `CastDevice` protocol (`tellybox/cast/device.py`): add `async def seek(self, position_s: float) -> None`.
- `PychromecastDevice` (`tellybox/cast/pychromecast_device.py`): implement it through `self._run("seek", lambda c: c.media_controller.seek(position_s))`, like `pause` and `resume`.
- `FakeCastDevice` (`tellybox/cast/fake.py`): re-base `_Media.pos` and `_Media.since` to the new position, keep the state, and emit a status event. Record the call in `calls`.
- `CastController.seek(delta_s=None, to_s=None)`:
  - exactly one of the two must be given; the target is `position + delta_s` or `to_s`;
  - clamp to `[0, duration - margin]`, with a margin of a few seconds, so a forward seek can never end the episode and trigger autoplay by accident;
  - does nothing (409 at the API) when nothing is playing;
  - is refused when the gate is closed (time is up and no grace), and works during the grace period;
  - is not a new pick: it never calls `timer.on_pick` and never starts a watch session;
  - saves the position straight after the seek (PB-4).
- Timer: no change. The `MediaStatus` that follows a seek goes through `_on_media`, which re-bases the position. BUFFERING already counts as playing (WT-3), so a seek does not stop the clock or double count.
- Cast API (`tellybox/cast/api.py`, `docs/cast-api.md`): `POST /seek` with `{"delta_s": 10}` or `{"to_s": 120}`; 409 when nothing plays or the gate is closed.
- Web (`tellybox/web/cast_client.py`): `CastClient.seek`. Kid API (`tellybox/web/kid.py`, `docs/kid-api.md`): `POST /api/kid/seek`, same body, returns a `KidState`; 409 when nothing is playing (the same `command()` path as pause and resume, 503 when the TV is unreachable).
- `KidState.now_playing` gains `position_s` and `duration_s` (`kid_state()` in `kid.py`), and `docs/kid-api.md` is updated. Because they change every second, the hub already republishes on each cast state change; the kid app interpolates between events from the last `position_s` while the state is `playing`.

### Receivers
- Default Media Receiver: seek works per the spike.
- Tellybox receiver: check the CAF SEEK default on the real device. If it is not supported, set `supportedMediaCommands` in `tellybox/web/receiver/cast.js` to include SEEK and keep the receiver protocol backward compatible with older servers (see the step 12 entry in `docs/PROGRESS.md`).

### Kid app (KA-2: no reading needed)
- The now-playing bar in `app.js` gets a progress bar and two icon buttons, back 10 s and forward 10 s. Icons go in `tellybox/web/static/icons.js`.
- Dragging or tapping the bar to seek is open decision 3; the plan builds the buttons first and the bar read-only.
- Disabled while the state is `loading` and while the TV is down, the same as pause.
- In the reader UI (step 16, KA-11) the bar also shows the position and duration as text.
- New labels (screen-reader names for the buttons) go in `tellybox/web/static/i18n.js` and the nl/de entries there.
- No volume and no skip controls: they stay excluded.

### Tests
- `tests/cast`, with `FakeClock` and `FakeCastDevice`: seek while playing, paused and buffering; clamped at 0 and at the end; with nothing playing; during the grace period; refused when time is up; the timer's playing seconds are not changed by a seek; the position is persisted; seek is not a pick (no new watch session).
- `tests/cast/test_fake_device.py` and `tests/cast/test_pychromecast_device.py`: `seek` on both devices.
- `tests/cast/test_api.py`: `POST /seek` (delta, to, both given, neither, nothing playing).
- `tests/web`: `FakeCast.seek` in `tests/web/conftest.py`, kid API tests for `POST /api/kid/seek` (200, 409, 503) and for `position_s`/`duration_s` in `KidState`.
- `tests/js`: a Node test for the progress and seek helpers, in the style of `split_mark.test.mjs`.
- `tests/test_i18n.py` must pass.

### Real-device check (owner in the loop)
Seek back and forward on the Default Media Receiver and on the Tellybox receiver, on the 1st-gen Chromecast: playback resumes within a few seconds, the sky on the TV and the time left in the app stay right, and a forward seek near the end does not skip to the next episode.

## Step 18: watching in the app (issue #31)

### Design
The cast service gets a virtual "app" session beside the Chromecast session. The page plays the file in a `<video>` and the server keeps counting time and applying the allowance, so the timer rules stay in one place.
- `Current` gets a `source` field (`"cast"` or `"app"`, default `"cast"`). One session at a time still holds (non-goal 3): a pick on either target replaces the current session.
- `POST /api/kid/play` accepts `"target": "app"` (default `"cast"`). The cast service runs the usual gate (`timer.on_pick`) and starts the watch session, but loads nothing on a device. It returns a session token and a media path.
- The page plays the media and posts heartbeats to `POST /api/kid/app/heartbeat`, proxied to a new cast API route. A heartbeat carries state (playing, paused, buffering, ended), position and duration. The page sends one on every state change and every 5 to 10 s while playing.
- The cast service maps heartbeats to `timer.set_activity` (PLAYING, PAUSED, STOPPED) and keeps `Current.position_s` and `player_state` up to date, so continue-watching (PB-4), history and the autoplay choice (PB-3, which tells the page the next episode) reuse the existing code.
- No heartbeat for about 20 s means STOPPED, the same as a lost connection: counting freezes. After the 15-minute idle rule (WT-3) the viewing session ends as it does for a cast.
- Stop-now, a block and the end of the grace period reach the page through the kid SSE stream as a field in `KidState` (for example `app_session: {"token": ..., "stop": true}`), because the hub sends unnamed events. The page tears down the `<video>` and shows the usual stopped or time's-up state.
- A cast service restart ends the app session on the next heartbeat (unknown token). Cast-style recovery does not apply.

### Kiosk lock, honestly scoped
A web page cannot lock down a device. What the server can enforce is that time is always counted and the allowance gate always applies to in-app playback.
- The app's media URL has a short lifetime (2 minutes, renewed by each heartbeat) and is bound to the session token. Today's 24-hour URL would let a copied link play without counting. `tellybox/media_urls.py` gets a second signer for this, and the `/media/{episode_id}/` path is still how our media is recognised.
- Client side: fullscreen, no native controls (our own bar from step 17), `controlsList="nodownload"`, and no picture-in-picture. These are conveniences, not security.
- Guided access (iOS), screen pinning (Android) or a kiosk browser stay the owner's own setup. They are documented in `docs/installation.md`, and the PRD environment row "no kiosk lock required" is amended.

### Profile setting
- Migration `015_profile_app_playback.sql` adds a per-profile setting (open decision 6: a boolean `app_playback_allowed`, default on, or a three-way `watch_mode`).
- Exposed in `tellybox/web/admin/settings_page.py` and `settings.html`, in the same pattern as `ui_mode` and the default TV (step 16), in `profile_order()` and the kid profile payload (`tellybox/web/kid.py`), and in `docs/kid-api.md`.
- Group rule: with a mixed group selected, the strictest member wins, like KA-11 (reader UI): in-app playback is offered only if every member allows it.

### Kid app
- A "play here" option next to the TV, icon only (KA-2). Where it lives is open decision 4.
- The in-app player is the existing now-playing bar plus a fullscreen `<video>`. The progress and seek buttons from step 17 apply to it; `seek` is just `video.currentTime` on the page, reported by the next heartbeat.
- New labels go in `i18n.js` and the nl/de entries.

### Step 18b: handoff (stretch)
Stop one session and start the other at `position_s` (the device `play` already has `start_s`). It reuses the session switch of step 18, so it is a separate PR after it. Open question for 18b: whether handoff is a button in the now-playing bar or part of the "play here" option.

### Tests
- `tests/timer` and `tests/cast`, with a fake clock and a fake app client: heartbeat sequences (play, pause, buffering, end), the 20 s timeout, the stop signal, gate refusal at pick, the grace period, replacement of an app session by a cast pick and the other way round, a restart ending the session, autoplay of the next episode.
- `tests/test_media_urls.py`: the short-lived URL, its binding to the token, expiry, and renewal.
- `tests/web`: `target: "app"` on `/api/kid/play` (200, 409 when out of time, 403 or hidden when the profile does not allow it), the heartbeat route, the settings page and migration, the group rule.
- `tests/js`: the heartbeat helper (interval, events, retries).
- `tests/test_i18n.py` must pass.

### Real-device checks (owner in the loop)
- A phone and a tablet, on iOS Safari and Android Chrome: fullscreen, the background tab pausing, screen lock, a heartbeat gap freezing the timer, a stop-now and time's-up ending playback on the device.
- A copied media URL stops working after about 2 minutes.
- Handoff (18b): a video moves between the phone and the TV at the same position.

## Proposed PRD wording

Proposals only. They ship with the implementation PRs (step 17's PR for seek, step 18's PR for the rest) and are not applied by this plan. The IDs assume step 16 and per-profile limits are merged first (KA-12, PB-6, A-23 taken).

**Seek (step 17)**
- KA-6, amended: "A now-playing bar shows the current episode thumbnail, a pause/resume button, a progress bar and back/forward buttons that seek 10 seconds. No volume or skip controls."
- KA-13 (new, Must, v9): "The kid app can seek in the episode that is playing: back and forward in steps of 10 seconds, and the bar shows how far the episode is. Seeking is not a new pick: it is not timed separately, it does not start a viewing session, and it is refused when picks are not allowed (KA-9). It cannot seek past the end, so it never triggers autoplay."
- CR-9 (new, Must, v9): "Both receivers support seeking: the Default Media Receiver and the Tellybox receiver. A seek is a command from the cast service to the device, and the playing time keeps counting through the buffering that follows (WT-3)."

**In-app playback (step 18)**
- PB-7 (new, Must, v9): "An episode can also be played in the kid app's own player instead of on the Chromecast. It is the same MP4 file, started by Tellybox, with the same allowance gate, timer, history, continue-watching and autoplay. One session plays at a time: a new pick on either target replaces the current one."
- KA-14 (new, Must, v9): "The kid app offers 'play here' next to the TV for a profile that allows in-app playback. It uses an icon and needs no text (KA-2). The setting is per profile, on by default. For a mixed group, in-app playback is offered only if every member allows it."
- WT-10 (new, Must, v9): "In-app playback is timed from heartbeats the page sends while it plays. The timer counts playing time only, the same as for a cast (WT-2, WT-3), and BUFFERING counts as playing."
- WT-11 (new, Must, v9): "If the page stops sending heartbeats for about 20 seconds, the session counts as stopped and counting freezes, as for a lost connection. Stop-now, a block and the end of the grace period stop the player on the device."
- WT-12 (new, Must, v9): "In-app media URLs are bound to the session and valid for only a few minutes, renewed while the session lives, so a copied link cannot play outside the timer."
- WT-9, reworded: "Only playback started through Tellybox is timed and controlled, on a Chromecast or in the app; other casts to the Chromecast are ignored."
- A-24 (new assumption): "Tellybox cannot lock a kid's phone or tablet. For in-app playback it guarantees that time is counted and the allowance applies; keeping a kid inside the app is done with the device's own guided access, screen pinning or kiosk mode (owner, 2026-10-03, if approved)."
- Non-goals: item 5 of the list is unchanged. The Users and environment row "Kid devices ... no kiosk lock required" (`docs/PRD.md:43`) becomes "no kiosk lock required; for in-app playback, an optional device lock such as guided access keeps a kid in the app (A-24)".
- Build order: add "17. Seeking" and "18. Watching in the app (18b: handoff)".

**CLAUDE.md hard rule carve-out (step 18)**
- "Never use the YouTube app or YouTube receiver on the Chromecast. Episodes are always MP4 files served by our server and cast to the Default Media Receiver. From v7, they go to our own Tellybox receiver instead, with the Default Media Receiver as automatic fallback (CR-6)." gets this added: "A profile may also play the same MP4 file in the kid app's own player (PB-7); that playback is still started and timed by Tellybox."
- "Only playback started by Tellybox is timed or controlled; ignore other casts." gets "(on a Chromecast or in the app)".
- The decisions list gets the in-app timing rules (heartbeats, 20 s timeout, short-lived URLs).

## How to build it

- The controller (the main Claude session) plans and reviews. Each part is built by a Sonnet subagent in its own worktree, and the controller reviews the diff before it is committed. One PR per part, on `main`:
  - Step 17: one PR (seek), branch `step17/seek`.
  - Step 18: one PR for in-app playback (branch `step18/in-app`) and a separate PR for 18b, handoff (branch `step18b/handoff`).
- Subagent briefs are written to `docs/plans/step17-handoff.md` and `docs/plans/step18-handoff.md`, as for earlier steps.
- Each part uses TDD: the failing test first (fake clock, `FakeCastDevice`, fake app client), then the code.
- Each part ends with the i18n step: new strings through `_()`, `t()`/`tn()` plus `tellybox/web/admin/js_strings.py`, or `i18n.js`; then `scripts/i18n.sh`, and nl/de translations in `tellybox/locale/{nl,de}`. `tests/test_i18n.py` must pass.
- The PRD, CLAUDE.md and `docs/PROGRESS.md` edits for a part go in that part's PR. Step 17's PR also updates `docs/cast-api.md` and `docs/kid-api.md`; step 18's PR also updates `docs/kid-api.md` and `docs/installation.md`.
- No tag or release without the owner's go-ahead (`docs/RELEASING.md`).
- Each part ends with the owner's real-device checks, listed above.

## Open decisions (for the owner)

1. Step numbers and release: steps 17 and 18 as proposed, or folded into v8? Step 11 (channel subscriptions) is still next in the build order, so should this wait for it?
2. Seek on both receivers, or the Tellybox receiver only?
3. Seek controls for kids: back/forward 10 s buttons only, a draggable bar, or both?
4. In-app "play here": a global switch in the kid app, or a per-profile default target?
5. Is the honest kiosk scoping (server-side counting plus a short-lived URL, no device lock) acceptable?
6. Per-profile in-app playback as a boolean (allowed or not), or a three-way setting (TV only, app only, both)?

## Bookkeeping

- This PR: the plan document and a `docs/PROGRESS.md` entry for steps 17 and 18 as planned.
- Implementation PRs: PRD requirements and build-order rows (above), CLAUDE.md edits (above), `docs/PROGRESS.md` updates, and the API docs.
