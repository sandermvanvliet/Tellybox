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
