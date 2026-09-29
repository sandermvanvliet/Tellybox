# Step 13 handoff: subagent briefs

These go with `docs/plans/step13-receiver.md`. Three subagents, run in parallel on **Sonnet**, each in its own git worktree off the part 0 contract commit. The controller (Opus) writes the contract, runs the spike on the real TV with the owner, then merges A, B, C and applies the spike findings.

## Rules for every subagent

- Read `CLAUDE.md`, `docs/plans/step13-receiver.md` (part 0, your part, "Tests") and the contract files in your brief before coding. The PRD (`docs/PRD.md`) is the source of truth; cite requirement IDs (CR-1..8, WT-*, NF-*) in tests and comments where the surrounding code does.
- Write the tests first (TDD) for Python code. No network, no real Chromecast: use the fake clock and `tellybox/cast/fake.py`.
- Run the tests from your worktree root with `/home/sander/Projects/Tellybox/.venv/bin/python -m pytest -q`. The full suite must pass before you report.
- Stay inside your files. If the contract is wrong or missing something, don't change it on your own: work around it minimally and report it.
- Match the surrounding code: naming, comment density, idioms. No new Python dependencies.
- The repository is public: no hostnames, IPs, internal domains, or the owner's Cast app ID.
- Commit in your worktree with a message ending in `Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>`. Don't push, and don't open a PR.
- **Report back:** what you built, the test count before and after, your worktree path, branch and commit, any contract gaps, and anything you're unsure about.

## Subagent A: cast service

**Files:** `tellybox/cast/`, `tellybox/store.py` (settings reading only), `tests/cast/`.

**Contract to read:**
- `docs/receiver-protocol.md`;
- `docs/cast-api.md` (the `receiver` block);
- the stubs in `tellybox/cast/device.py` (`play(..., app_id=None)`, `send_receiver_message`, `ReceiverMessage`, `ReceiverUnavailable`, `RECEIVER_LAUNCH_TIMEOUT_S`, `RECEIVER_NAMESPACE`);
- `tellybox/migrations/006_receiver.sql`.

**Tasks**
1. **pychromecast adapter** (`pychromecast_device.py`):
   - With `app_id` given and not the running app, call `cast.start_app(app_id)` and wait until the receiver status shows it, up to `RECEIVER_LAUNCH_TIMEOUT_S`. A launch error (from the launch-error listener) or a timeout raises `ReceiverUnavailable`. Then load through `cast.media_controller.play_media` as today.
   - A `TellyboxController(pychromecast.controllers.BaseController)` on `RECEIVER_NAMESPACE`, registered with `cast.register_handler`. `receive_message` posts `ReceiverMessage(payload)` events. `send_receiver_message` sends JSON when our app is the running app; otherwise it does nothing and logs at debug.
   - Mirror pychromecast's API carefully; find its source with `.venv/bin/python -c "import pychromecast, os; print(os.path.dirname(pychromecast.__file__))"`.
2. **Fake device** (`fake.py`): supports a custom app id, launch-failure injection, a `sent_messages` list, and a `hello` `ReceiverMessage` after launching our app.
3. **Controller** (`controller.py`):
   - **WT-9:** replace the four `== DEFAULT_MEDIA_RECEIVER` checks with an "is ours" helper that accepts the Default Media Receiver or the configured app id.
   - **Settings:** `receiver_app_id` is read from settings together with the timer settings (on start and each persist).
   - **Fallback (CR-6):** pass the app id to `device.play` unless `fallback_until` is in the future. On `ReceiverUnavailable`, record `last_error` and set `fallback_until = now + 30 min`, then play the same pick with `app_id=None` (the Default Media Receiver).
   - **Messages:** build them exactly as in `docs/receiver-protocol.md`:
     - `state` with the watchers' sky (`fraction_left` as in `tellybox/web/kid.py::_fraction_left`, for the watchers);
     - `time_up`;
     - `loading`, sent before each load, including autoplay;
     - `up_next`, only when autoplay will continue.
   - **Throttle:** send on changes of phase, `time_up`, now-playing, `loading` or `up_next`, when `fraction_left` moves by 0.01 or more, and every 30 s at least while our app runs. Reply to `hello` with the current state.
   - **Night (CR-3):** when our receiver runs and the episode ends because time is up (`FINISH_THEN_STOP` at the end, `STOP_NOW` from block, stop-now or grace), don't call `device.stop()`. Send `time_up` and schedule a quit after `NIGHT_HOLD_S` (600). A new load cancels it. Other ends (end of show, a parent stop without time up) keep today's `device.stop()`.
   - **Stats (CR-8):** log `stats` messages at info.
   - **State:** add the `receiver` block to `state()`.
   - With no app id configured, behaviour and messages are exactly as today: no `send_receiver_message` calls.

**Tests to add:** the "fake device and controller" list under "Tests" in the plan.

## Subagent B: receiver page

**Files:** `tellybox/web/receiver/` (except `spike*.html`, which are the controller's). Nothing else.

**Contract to read:** `docs/receiver-protocol.md` (messages and states); `tellybox/web/static/sky.js` and `app.css` (the kid app's sky colours, sun position and moon, which the TV must match).

**Tasks**
1. `index.html`, `receiver.css`, `ui.js`, `cast.js`:
   - `ui.js` has no Cast SDK. It exports `view(tb, player)`, where `tb` is the last `state` message and `player` is `{state, currentTime, duration}`, and `render(view)`, plus the sun position (port `sunPosition`/`phase` from `sky.js`, as a copy; the receiver can't import from `/static` when hosted on Pages).
   - The layers are: playing (video plus the corner sky, which is hidden when unlimited), loading, up-next card, night, and idle (a day sky). They are defined in `docs/receiver-protocol.md`.
   - `cast.js` is the CAF glue:
     - load `//www.gstatic.com/cast/sdk/libs/caf_receiver/v3/cast_receiver_framework.js`;
     - start `CastReceiverContext` with `customNamespaces` (JSON) and `disableIdleTimeout: true`;
     - send `hello` on `SENDER_CONNECTED`;
     - forward `state` messages to `ui.js`;
     - map `PlayerManager` events (LOAD_START, BUFFERING, PLAYING, PAUSE, MEDIA_FINISHED, ERROR, TIME_UPDATE) to `player`;
     - send `stats` every 60 s with dropped and total frames from the video element (`getVideoPlaybackQuality()` or `webkitDroppedFrameCount`/`webkitDecodedFrameCount`).
   - Hide the CAF player's own UI and splash with `cast-media-player` CSS variables and styling, so only our layers show.
2. **CR-8 rules:**
   - gradients as static backgrounds;
   - no `filter`, `backdrop-filter`, `box-shadow` or blur over the video;
   - the sun moves by `transform` only;
   - no CSS animation while the video plays; the loading pulse animates `opacity` only;
   - the corner sky is about 12% of the width.
3. **No text anywhere** (KA-2). Images are `<img>` tags with the URLs from the messages; a failed image shows the plain layer without it.
4. **`dev.html`:** loads `ui.js` without the SDK and has buttons for each state (day, dusk, unlimited, loading, up-next, night, idle), plus a 1280×720 stage with a sample `<video>` or a coloured placeholder.
5. **Screenshots:** take them with Playwright at 1280×720 into `screenshots/` in your worktree (not committed), and list their paths in your report. Look at them yourself and fix what's off.

## Subagent C: web, admin and docs

**Files:** `tellybox/web/app.py` (the `/receiver/` mount only), `tellybox/web/admin/` (settings and dashboard), `tellybox/locale/`, `docs/installation.md`, `tests/web/`.

**Contract to read:** `docs/cast-api.md` (the `receiver` block), `tellybox/migrations/006_receiver.sql`, and `docs/receiver-protocol.md` (for the docs section).

**Tasks**
1. Serve `tellybox/web/receiver/` at `/receiver/` with `Cache-Control: no-cache`, the same way `/static` is mounted, plus a test.
2. **Settings:** a "Tellybox receiver app ID" field.
   - It is optional; the value is uppercased and must match `^[0-9A-F]{8}$`.
   - The help text says empty means the Default Media Receiver, and links to the installation guide section.
   - It is stored in `settings.receiver_app_id`, with an empty value stored as NULL.
3. **Dashboard:** a "TV receiver" line from the cast state's `receiver` block, updated live in `dashboard.js`, escaped, with the time in the admin's local format. It reads:
   - "Tellybox receiver";
   - "Default Media Receiver";
   - "Default Media Receiver (Tellybox receiver unavailable until %(time)s: %(reason)s)".
4. **i18n:** mark every string, list the JS strings in `js_strings.py`, run `scripts/i18n.sh` (with `PYBABEL=/home/sander/Projects/Tellybox/.venv/bin/pybabel`), and translate the nl and de entries informally. `tests/test_i18n.py` must pass.
5. **`docs/installation.md`:** an optional "Tellybox receiver" section for self-hosters:
   - register in the Google Cast SDK Developer Console (one-time fee);
   - add a Custom Receiver whose URL is your fork's GitHub Pages `…/receiver/` (enable Pages with the Actions source) or your own HTTPS host serving `/receiver/`;
   - add the Chromecast's serial, wait, and reboot it;
   - enter the app ID in Settings;
   - what happens on failure (the automatic fallback).

   Keep it general: no owner details.

## Controller checklist after the subagents

1. Merge A, B, C into `step13/receiver` (after the part 0 PR is merged), then apply the spike's findings to the adapter launch and `receiver/cast.js`. Remove the spike pages.
2. Run the full suite, `scripts/i18n.sh` and `tests/test_i18n.py`.
3. Review:
   - WT-9 with two app ids;
   - the fallback path and window;
   - the night hold and cancel;
   - the message size and throttle;
   - that no private details are committed.
4. Screenshots for the owner (a private artifact page), then the PR. After deploy, the real-device checks in the plan.
5. Update `docs/PROGRESS.md` and the PRD status for v7.
