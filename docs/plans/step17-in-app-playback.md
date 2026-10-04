# Plan: Watch in the app instead of casting (issue #31)

## Context
Kids can only watch on the TV via Chromecast. #31 adds in-app playback on a phone/tablet: full screen, library on return, limits enforced server-side. It cuts across the single-session cast controller and the PRD (KA-5, KA-6, PB-6), so the PRD goes first. Delivery is two PRs (CLAUDE.md: one step per PR), proposed as step 17.

## Decisions (from the grilling)
- **PRD**: add new requirements (KA-*, PB-*, WT-*) plus a PROGRESS.md entry before any code. PB-6 ("no TV picker") and KA-5/KA-6 are amended.
- **Toggle**: per device (localStorage), big TV/phone icon, no reading. Default TV. An admin switch per profile, "allow watching in the app", default off, hides the toggle.
- **Sessions**: one TV session (new TV pick replaces it, A-5). Browser sessions are per device, each holding one profile group. A profile is in at most one session: starting anywhere ends its other session, and a group-mate's session ends with it (TV to phone stops the TV at once).
- **Timing** (WT-*): heartbeat every 10 s with state and position. No heartbeat for 30 s stops counting (lost connection); no heartbeat for 5 min ends the session. A reported pause is a pause (15 min rule). Per-heartbeat credit is capped.
- **Limits**: same as TV: grace-capped finish-the-episode, max session length, unlimited-today, and immediate block/stop-now. Enforced by revoking the media token and by the heartbeat response. Non-text "time's up" screen.
- **Media URL**: relative `/media/...` (same origin as the page), a new shorter-lived signed path scoped to the session. Chromecast URLs unchanged.
- **Ending**: autoplay next per show (PB-3) as on TV; library after the last episode or time-up.
- **Controls**: native `<video>` controls including seeking and fullscreen (KA-6 is amended). An episode is marked finished only at >=95% position AND accumulated played time >= ~50% of duration (PB-4 gated).
- **Failures**: load/decode error shows a non-text error icon, reports to the server, counts no time, no TV fallback.
- **Admin**: stop-now/block/unlimited apply to browser sessions (HA-8, via the cast service). The admin dashboard and API state list all sessions with their target (TV or a user-agent label like "iPhone Safari"). History (21 days) records the target. Each device's now-playing bar shows its own group's session; the TV session shows everywhere.

## PR 1: multi-session refactor (no visible change)
- `tellybox/cast/controller.py`: `self.current` (single `Current`, line ~217) becomes sessions keyed by profile group, with the TV session as one resource. Update `play` (259), `_start_episode` (572), `_on_media` (487), `_on_finished` (536), `tick` (358), `persist` (405), `_apply_decision` (797).
- `tellybox/timer/watch_timer.py`: stays pure; confirm per-profile accounting (WT-3, A-13) already supports concurrent profiles, and extend with fake-clock tests.
- `docs/cast-api.md`, `tellybox/cast/api.py`, `tellybox/web/cast_client.py`, `tellybox/web/kid.py` (`kid_state`): state exposes a list of sessions, with the old shape kept for the TV.
- Proof: the existing suite stays green, plus new tests for overlapping-profile replacement.

## PR 2: browser player
- Cast service: a `BrowserDevice`-style adapter (implements the `CastDevice` protocol in `tellybox/cast/device.py`, fake in `fake.py` for tests) fed by a heartbeat endpoint; it synthesises MediaStatus-equivalent events. Missing-heartbeat handling follows the `_lost_at`/`RECONNECT_WAIT_S` path.
- `tellybox/media_urls.py`: add the shorter-lived, session-scoped signed path; `tellybox/web/app.py` media route (115-127) verifies it and checks the allowance.
- Kid API (`tellybox/web/kid.py`, `docs/kid-api.md`): `play` accepts a target and returns a relative URL for the device; new heartbeat/end endpoints.
- Kid frontend (`tellybox/web/static/app.js`, `api.js`, `icons.js`, `i18n.js`): toggle, `<video>` fullscreen view, error and time's-up screens, return to the library.
- Admin: per-profile switch (migration like `013_profile_ui_tv.sql`), sessions list, history target column. All new strings via `_()`, `t()`, `i18n.js`, then `scripts/i18n.sh` and nl/de translations.
- Docs: `docs/admin-api.md`, PRD, PROGRESS.md.

## Verification
- Unit: timer tests with a fake clock and fake devices (heartbeat gaps, 30 s/5 min thresholds, grace, stop-now/block, the finished-mark gate, profile replacement across targets). `tests/test_i18n.py` must pass.
- Run the app (`run` skill) and drive the kid app in a browser (Playwright): toggle, play, fullscreen, end, time's-up.
- Owner-in-the-loop checks at the end of each PR: real Chromecast, iPhone Safari and an Android phone, including screen lock and Wi-Fi loss.

## Slicing of the browser player (refines "PR 2")
- **2a cast service** (`step17c-device-sessions`): device sessions in the controller, `/device/*` cast API, scoped media URL helpers, `watch_session.target`/`device_label`, per-playback decisions. No web or UI.
- **2b web backend + admin**: kid API (`play` with a target, heartbeat/stop proxies, `sessions` in KidState), media route for scoped URLs, `profile.watch_in_app` (AD-7) with its admin switch, sessions on the dashboard/admin API, history target (AD-4).
- **2c kid app**: toggle (KA-13), player view (KA-14), error and time's-up screens.
- Each is its own PR, built in this order. The controller keeps `current` for the TV; device sessions live beside it.

## Contract between the slices
Timer keys: `TV` ("tv") and `"device:<device_id>"`. `device_id` is a random id the browser keeps in localStorage (letters, digits, `-`, `_`, 8..64 chars).

**Cast API (localhost, `docs/cast-api.md`)**
- `POST /device/play {device_id, label, episode_id, profile_ids}` -> 200 `{session: Session, url: "/media/<episode_id>/<expires_at>/<sig>.mp4?s=<watch_session_id>", start_s}`; 404 no such episode; 422 unknown profile or bad device_id; 409 `{error:"time_up", reason}`. `label` is a short browser label ("iPhone Safari"), at most 40 chars. Replaces any session of the same device (REPLACED) and any session of the profiles on other targets (PB-8; their next heartbeat answers `stop` with reason `replaced`). Does not need a Chromecast.
- `POST /device/heartbeat {device_id, state: "playing"|"paused"|"buffering"|"ended"|"error", position_s, duration_s?}` -> `{action: "continue"|"stop"|"next", reason: null|"time_up"|"blocked"|"stop_now"|"replaced"|"disconnected"|"error"|"finished"|"unknown_session", time_up: bool, grace_deadline: iso|null, next?: {session, url, start_s}}`. `ended` finishes the episode and, when autoplay is allowed (PB-3, WT-4), answers `next` with the next episode's session and URL; otherwise `stop` with reason `finished` or `time_up`. `error` ends the session (LOAD_FAILED) without counting time. Unknown device or no open session: `stop` / `unknown_session` (200, not an error).
- `POST /device/stop {device_id}` -> 200 state; ends the device's session (STOPPED), saves the position. Unknown device is a no-op.
- State (`GET /state`, SSE) gains `sessions: [Session]`; `now_playing`/`device` stay the TV exactly as today. `Session = {key, target: "tv"|"device", label|null, device_id|null, episode_id, show_id, title, state: "loading"|"playing"|"paused"|"buffering", position_s, duration_s, profile_ids}`; the TV session appears with `key:"tv"`, `target:"tv"`, label = the TV name. `timer.profiles[].watching` is true for any session.
- `pause`/`resume`/`stop` (existing, TV-only) are unchanged. `override stop_now`, `block` and time-up end every affected session; block and stop-now immediately, time-up through the per-key decision with grace.

**Timing (WT-10, WT-11)**: a heartbeat sets the activity of `device:<id>`. No heartbeat for 30 s -> activity STOPPED (stops counting); 5 min -> session ends DISCONNECTED, position kept up to the last heartbeat. Per-key decisions drive STOP_NOW / FINISH_THEN_STOP (grace deadline in the heartbeat answer; the server answers `stop` once the decision is STOP_NOW). Heartbeats from a tab that reports `playing` while nothing advances are not detectable server-side; accepted (A-29).

**Finished (PB-8)**: a device episode is marked finished only at >=95% position and accumulated PLAYING time >= 50% of its duration; `ended` after a seek with less played time saves the position but not the finished mark.

**Media URLs**: `media_urls.media_path(secret, episode_id, expires_at, session_id=None)`; the signature covers `episode:expires[:session]`. A URL with `?s=` is valid only while that `watch_session` row is open (`ended_at IS NULL`, target `device`); the web media route checks it in the shared DB, so stop-now/block/time-up revoke it. TTL stays 24 h. `episode_id_from_url` still works.

**DB**: migration `016_watch_session_target.sql`: `watch_session.target TEXT NOT NULL DEFAULT 'tv'`, `watch_session.device_label TEXT`. Restart recovery only re-attaches `target = 'tv'`; open device sessions are closed with RESTART at start.
