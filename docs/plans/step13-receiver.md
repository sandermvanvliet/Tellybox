# Step 13: Tellybox receiver (v7, CR-1..CR-8)

Status: approved 2026-09-29. Part 0 merged as PR #2 (`step13/receiver-contract`); parts A, B and C built and merged on `step13/receiver` (pushed, no PR yet). Waiting for the spike; see "Resume here" in `docs/PROGRESS.md`.

## Why now

The owner moved v7 ahead of v3–v6 on 2026-09-29: the TV is where the kids look, and today it shows the Chromecast's spinner and backdrop, with nothing about time. The PRD build order is updated to match.

## Decisions (owner, 2026-09-29)

- **Hosting:** the spike tries GitHub Pages first, because it needs no DNS change and the receiver's HTML and JS contain no household data. If the 1st-gen Chromecast blocks plain-HTTP media or images from an HTTPS page, the spike tries the home server under a public DNS name with a real certificate instead (media then over HTTPS too).
- **Scope:** all of CR-1..CR-8. The Shoulds, the loading screens (CR-4) and the up-next card (CR-5), are separate pieces in the receiver and the cast service, so they can slip without blocking the rest.

## What already exists

- `tellybox/cast/device.py`: the `CastDevice` protocol. `play()` launches the Default Media Receiver (`DEFAULT_MEDIA_RECEIVER = "CC1AD845"`) and loads a signed MP4.
- `tellybox/cast/pychromecast_device.py` uses `cast.media_controller`, a pychromecast `MediaController` with `app_must_match=False`. It loads into whatever running app implements the standard media namespace, as a CAF receiver does, and launches the Default Media Receiver only when the running app doesn't. A `BaseController` subclass registered with `cast.register_handler` carries a custom namespace. No new dependency is needed.
- `tellybox/cast/controller.py` recognises our playback (WT-9) by `app_id == DEFAULT_MEDIA_RECEIVER` in four places, plus the receiver session id and our `/media/{episode_id}/` URL.
- The kid app's sky (`tellybox/web/static/sky.js`, `app.css`) maps state to day, dusk, night and unlimited, and gives the sun's position. The TV sky reuses the same mapping and colours.
- The kid image routes (`/img/episode/{id}.jpg`, `/img/show/{id}.jpg`) are on the web service, next to `/media/`, at `TELLYBOX_MEDIA_BASE_URL`.

## Part 0: contract and spike tools (controller)

1. **Migration `006_receiver.sql`:** adds `settings.receiver_app_id TEXT`, nullable. Empty means the Default Media Receiver only, which is the v1 behaviour and the default for other self-hosters.
2. **`docs/receiver-protocol.md`:** the custom namespace `urn:x-cast:tellybox`, the JSON messages in both directions, and what the receiver shows in each state.
3. **Device protocol stubs** (`device.py`, `fake.py` signatures):
   - `play(url, *, title, start_s, app_id=None)`;
   - `send_receiver_message(payload)`;
   - a `ReceiverMessage` event;
   - a `ReceiverUnavailable(CastCommandError)` exception.
4. **`docs/cast-api.md`:** the state gains a `receiver` block.
5. **Receiver directory** `tellybox/web/receiver/`, served by the web service at `/receiver/` and published to GitHub Pages by `.github/workflows/receiver-pages.yml` on changes to it. For the spike, it holds `spike.html` (CAF) and `spike-v2.html` (the legacy v2 SDK with its own `<video>`, in case CAF doesn't run on the 1st gen).
6. **`scripts/receiver_spike.py`:**
   - launches an app id on the Chromecast and loads a signed media URL from the dev web service;
   - talks to the page over the namespace, printing the receiver's log, user agent, errors and dropped-frame counts;
   - measures launch and tap-to-playing times.
7. **PRD and progress log:** the build-order change in the PRD, and step 13 "in progress" in `docs/PROGRESS.md`.

## Spike (controller with the owner, on the real TV)

**Owner prep** (the only blocker):
1. Merge the part 0 PR, so Pages publishes the spike page.
2. Register at the Google Cast SDK Developer Console (a one-time US$5 fee).
3. Add a Custom Receiver app with the spike page's Pages URL.
4. Add the Chromecast's serial number as a test device, wait about 15 minutes, then reboot the Chromecast.
5. Tell the controller the app ID. It's configured in admin Settings, never committed.

**Checks, recorded in `docs/spike-receiver.md`:**

| # | Check | If it fails |
|---|---|---|
| S1 | The Pages spike page launches on the 1st gen; measure the launch time | Try the home-server hosting |
| S2 | CAF runs; note the user agent and Chrome version | Switch the console URL to `spike-v2.html` (legacy SDK, our own `<video>`) |
| S3 | HTTP media from the LAN plays from the HTTPS page, and HTTP images show | Home-server hosting, with media and images over HTTPS |
| S4 | A namespace round trip with pychromecast: the receiver says `hello`, gets state and echoes it | Blocks v7; rethink |
| S5 | `media_controller.play_media` loads into our app, not the Default Media Receiver; the `app_id`, session id and `content_id` in status are as the controller expects (WT-9) | Adjust the adapter |
| S6 | CR-8: a 720p clip for 2 minutes with the corner sky and sun moving every 5 s, against no overlay: dropped frames and stalls | Simplify the overlay: no gradients, smaller, fewer updates |
| S7 | With `disableIdleTimeout`, the page stays up 10+ minutes with no media; `quit_app` returns to the backdrop | Close sooner; note the limit |
| S8 | A wrong or unregistered app id: how pychromecast reports it and how long that takes | Sets the fallback timeout |
| S9 | Tap-to-playing, cold and warm, against the Default Media Receiver (NF-5) | Launch our app when a kid page opens (warm-up) |

After the spike, the controller adapts the thin seams: the SDK glue in `receiver/cast.js` and the launch in the pychromecast adapter. Then the spike pages are removed.

## Part A: cast service (subagent A)

- **Adapter** (`pychromecast_device.py`):
  - `play(..., app_id)`: if an app id is given and not running, `start_app(app_id)` and wait for it, with the launch timeout from the contract. Then load through `media_controller`.
  - A launch error or timeout raises `ReceiverUnavailable`.
  - A `TellyboxController(BaseController)` on `urn:x-cast:tellybox` delivers incoming messages as `ReceiverMessage` events. `send_receiver_message` is fire-and-forget, and does nothing when our app isn't running.
- **Fake device:** our app id, launch-failure injection, a record of sent messages, and a `hello` from the receiver after launch.
- **Controller:**
  - **WT-9:** "our session" means the app id is the Default Media Receiver *or* the configured receiver.
  - **Fallback (CR-6):** on `ReceiverUnavailable`, play the same pick on the Default Media Receiver at once. Keep using the Default Media Receiver for 30 minutes (`fallback_until`), then try again. Log the reason.
  - **State push (CR-2, CR-7):** send `state` on connect, on `hello`, on each change of phase, now-playing or up-next, when `fraction_left` moves by 1% or more, and at least every 30 s while our app runs. `fraction_left` is computed for the watchers, as in `kid.py`.
  - **Loading (CR-4):** before each load, including autoplay, send `loading` with the show artwork and episode thumbnail URLs.
  - **Up-next (CR-5):** `up_next` carries the next episode's thumbnail only when autoplay will continue: autoplay is on for the show, a next episode exists, and the watchers' decision allows autoplay.
  - **Night (CR-3):** when the episode ends because time is up (including block and stop-now) and our receiver runs, don't quit the app. Send `time_up`, keep the night on the TV, and quit after 10 minutes, or earlier if a new pick loads. Other ends (end of show, stop) quit as today. With the Default Media Receiver, everything behaves as today.
  - **Stats (CR-8):** log dropped-frame counts from the receiver.
- **State:** `receiver: {"kind": "tellybox" | "default", "configured": bool, "fallback_until": iso | null, "last_error": str | null}`.
- **Settings:** `receiver_app_id` is read with the timer settings on each persist, so a change in admin takes effect without a restart.

## Part B: receiver page (subagent B)

Static files in `tellybox/web/receiver/`, vanilla JS and CSS, no build step, and no text anywhere (KA-2).

- `index.html` and `receiver.css`: the video layer, a small sky in the top-right corner (about 12% of the screen width) with the sun and dusk states from the kid app, and full-screen layers:
  - **night:** a navy sky with the moon and stars, and the hill;
  - **loading:** the show artwork, large, with the episode thumbnail and a slow, gentle pulse;
  - **up-next:** a card in the bottom-right corner with the next thumbnail;
  - **idle:** a day sky.
- `ui.js` is pure, with no Cast SDK. `view(state, player) -> {layer, sky, upNext}` and `render(view)`. The up-next card shows in the last 10 s of media when `up_next` is set. The corner sky is hidden when unlimited (CR-2).
- `cast.js` is the thin CAF glue:
  - the `CastReceiverContext` with `customNamespaces` and `disableIdleTimeout`;
  - a `hello` on sender connect;
  - `state` messages go into `ui.js`, and player events (LOADING, BUFFERING, PLAYING, PAUSED, IDLE, time updates) go into `ui.js` too;
  - every 60 s, a `stats` message with dropped and total frames;
  - the CAF player's own UI is hidden.
- **CR-8 performance:**
  - gradients are drawn once;
  - no filters, blur or shadows over the video;
  - the sun moves with `transform` only, at most every 30 s, with no animation while playing;
  - the loading pulse runs only when no video is playing.
- `dev.html`: a desktop harness that drives `ui.js` with buttons and a sample video, for screenshots at 1280×720. The screenshots cover: playing with day, dusk and unlimited, loading, up-next, night, and idle.

## Part C: web, admin, hosting and docs (subagent C)

- **Web service:** serves `/receiver/` from `tellybox/web/receiver/` (`no-cache`; no auth, like the kid app). This is for the home-server hosting option.
- **Admin Settings:** a "Tellybox receiver app ID" field. It takes 8 characters of `[0-9A-F]` (uppercased) and may be empty, with help text saying that empty means the Default Media Receiver.
- **Dashboard:** a "TV receiver" line:
  - "Tellybox receiver";
  - "Default Media Receiver";
  - "Default Media Receiver (Tellybox receiver unavailable until 14:32: reason)".
- **i18n:** nl and de.
- **`docs/installation.md`:** an optional "Tellybox receiver" section:
  - register your own app in the Cast console;
  - the Pages URL of your fork or your own HTTPS host;
  - add the Chromecast serial and reboot;
  - enter the app ID in Settings.
- **The Pages workflow** is finalised by the controller in part 0; subagent C only documents it.

## Execution (at most 3 subagents, cheaper model)

1. **Controller (Opus):** part 0 on `step13/receiver-contract`, then a PR for the owner to merge. Enable Pages (Actions source) on the repository.
2. **Three Sonnet subagents in parallel,** in worktrees off the contract commit, with the briefs in `docs/plans/step13-handoff.md`. A codes against the fake device, B against its own harness, C against the cast state contract.
3. **The spike** with the owner, in parallel with the subagents. It uses only part 0.
4. **Controller:**
   - merge A, B, C into `step13/receiver`;
   - apply the spike findings to the seams;
   - full suite and i18n;
   - review (WT-9 with two app ids, the fallback path, image and media URLs on the TV, the 10-minute night);
   - screenshots for the owner, then the real TV.

## Tests

- **Fake device and controller:**
  - playing on our receiver;
  - launch failure → the same pick on the Default Media Receiver, and the 30-minute fallback window;
  - our receiver's session counts as ours, and a foreign app is still "taken over" (WT-9);
  - the state push triggers and throttle;
  - `hello` gets the state;
  - loading before autoplay;
  - up-next only when autoplay will continue;
  - the night hold, and the quit after 10 minutes on the fake clock;
  - a new pick during the night hold;
  - no app id means v1 behaviour, with no messages.
- **Admin:** app id validation, and the dashboard receiver line.
- **Web:** `/receiver/` is served.
- **Receiver:** reviewed through the `dev.html` screenshots, then on the TV.

## Real-device checks (owner)

1. Pick an episode: the TV shows the loading screen with artwork, then the video with the corner sky.
2. The sky sinks as time runs down, and turns to dusk in the last 5 minutes.
3. At the end of an episode with autoplay, the up-next card shows the next thumbnail, then the loading screen, then the next episode.
4. Time runs out: the episode finishes, and the TV shows the night scene. After 10 minutes it goes back to the backdrop and can sleep.
5. Unlimited today: there's no corner sky.
6. Fallback: clear the Pages site or enter a wrong app ID. Playback still works through the Default Media Receiver, and the dashboard says so.
7. Watch a whole episode: there's no visible stutter, and the logged dropped frames stay low (CR-8).
8. A YouTube cast from a phone is still ignored and recorded as taken over (WT-9).
