# Progress

Running log of the build order (see `docs/PRD.md`). PR numbers up to #17 refer to the private repository where Tellybox was developed before it was published on 2026-09-28.

## Done

### 1. Casting spike (2026-09-27, PR #1)
- Discovery, casting, pause/resume/seek, finish, foreign-cast takeover, power loss and restart re-attach all verified on the real 1st-gen Chromecast from a host-networked container.
- Tap-to-playing: 3.8–4.3 s cold, 0.9 s with the receiver already loaded (NF-5 target 5 s).
- Details: `docs/spike-casting.md`. The spike code was replaced in step 2.

### 2. Cast controller and timer (2026-09-27, branch `step2/cast-controller`)
- `tellybox/timer/`: pure watch timer (WT-1..WT-7), both counting modes, viewing session and max length, grace cap, overrides, DST-safe reset, snapshot/restore.
- `tellybox/cast/`: pychromecast adapter (remembered host first, mDNS fallback), fake device, `CastController` (autoplay, positions, end reasons, persistence every 15 s, WT-9 filtering, restart recovery), internal API on `127.0.0.1:8081` with SSE.
- `tellybox/web/`: signed media route only (NF-3). `tellybox/cli.py`: `migrate`, `dev make-clips`, `dev seed`.
- `compose.yml` dev stack (web + cast, host networking).
- 189 tests (fake clock, fake Chromecast).
- Verified on the real TV: auto-selecting the only Chromecast, pick to playing in 4.1 s cold, autoplay, time up mid-episode then finish and stop, refusal while time is up, extra minutes, stop now, restarting the cast container mid-play (re-attached within ~1 s), a YouTube cast from a phone recorded as taken over and not counted, 242 SSE updates.

### 3. Ingest pipeline (2026-09-27, branch `step3/ingest`)
- `tellybox/ytdlp.py`: yt-dlp as a subprocess from an updatable install in `<data>/tools/yt-dlp` (with Deno), image copy as fallback; preview, download with progress, error classification, atomic updates.
- `tellybox/media_format.py`: probe, remux-or-encode (x264 veryfast, low priority, ≤720p), verify.
- `tellybox/jobs.py`: SQLite job queue with backoff retries and restart recovery. `tellybox/ingest.py`: add = approve; download → process → publish, with only verified files moved into place (NF-8).
- `tellybox/worker/`: `worker` service; daily yt-dlp update at 03:00 and on request.
- Library: show per channel, rename, merge, hide, disk usage, delete with files.
- CLI: `add URL [--hold]`, `jobs`, `retry`, `ytdlp-update`.
- 319+ tests, no network.
- Verified for real:
  - First worker start installed yt-dlp 2026.08.19 + Deno into the data volume in 8 s, without a rebuild.
  - A 2:06 Bluey minisode: preview, download 4.6 s, remux 0.2 s (YouTube served H.264 Main@3.1 720p + AAC), published in the channel's show with a thumbnail, played on the TV.

### 4. Kid app (2026-09-27, branch `step4/kid-app`)
- Design: the sky is the timer. The sun sinks as the allowance runs down, dusk in the last five minutes, night with moon and stars when time is up. No visible text (KA-2).
- Frontend: vanilla JS modules + CSS in `tellybox/web/static/`, hash router, EventSource live state, installable manifest. `scripts/kid_mock_server.py` renders every state for design work.
- Backend: kid API in the web service (`docs/kid-api.md`): home with continue watching and up next, show pages, play/pause/resume, images, and one shared relay of the cast service's live stream (`KidHub`). Hidden/held content never appears, plays or serves an image.
- 406 tests. Screenshots of every state at three sizes were reviewed.
- Verified with the owner's phone and laptop:
  - Tap to TV in 3.97 s.
  - Pause on one device and play on another; the other screen followed about 1 ms after the Chromecast confirmed.
  - Cutting the allowance mid-episode showed night on both screens, tiles couldn't be tapped, Bluey finished and the TV went idle, and morning came back after restoring the allowance.
- Fix found during the check: open live streams blocked web/cast shutdown for 10 s; both now use a 2 s graceful-shutdown timeout.

### 5. Admin pages (2026-09-28, PR #5)
Plan: `docs/plans/step5-admin.md`, approved 2026-09-28. Built with a shared contract first, then three parallel slices (cap of 3 subagents), integrated and reviewed by the controller.
- Sign-in (AD-1, NF-2), in `tellybox/auth.py` and `tellybox/web/admin/`:
  - The password comes from `TELLYBOX_ADMIN_PASSWORD`(`_FILE`) and is stored as an argon2id hash. Changing it ends every session; with none set, the admin is locked.
  - Sessions: an HttpOnly, SameSite=Strict cookie on `/admin`; only a hash of the token is stored; 30 days sliding (in the DB and in the cookie).
  - Every POST passes an Origin check. Failed logins are refused for 1 s, doubling up to 60 s per address.
- Pages (Jinja2, phone-first, vanilla JS for the live parts):
  - Dashboard: SSE relay of the cast state, plus polling for jobs, disk and yt-dlp.
  - Add video, with a server-side preview. Jobs, with live progress, retry and the yt-dlp update.
  - Library and show pages: moving episodes between shows (LM-1), up/down reorder, publishing held downloads, artwork and thumbnails from an upload or an ffmpeg frame (LM-2), delete with files.
  - Settings, validated strictly. Chromecast search and select through the cast API.
  - History of the last 21 watch days, with the overrides applied.
- The worker purges history, timer days and the override log after 21 days, and finished jobs after 30, once per 03:00 day (AD-5). Signed media URLs are redacted in the access log.
- Review fixes before commit:
  - Video titles are escaped in the dashboard's live DOM updates (XSS).
  - Device search gets a 15 s timeout, since discovery takes 8 s.
  - Admin images revalidate on each view.
  - Upload reads are capped.
  - The purge schedule is independent of the yt-dlp update.
  - The worker and the web history page don't import code they don't need.
- 604 tests.

#### Real-device checks for the admin pages (2026-09-28)
- Passed on the owner's phone: sign-in, add video with progress on Jobs, rename, +30 min, block and unblock, history.
- Found and fixed: concurrent admin requests shared one SQLite connection. The result was 500s, a spurious "Admin is locked" and spurious sign-outs. The web service now uses one connection per thread; a burst of 900 requests all succeed.
- Still to check: hiding an episode shows in the kid app.

### Fix: a replacing pick ended its own session (2026-09-28, PR #6)
- **Found in the step 5 check:** a pick that replaced media on the receiver ended 60–90 ms later as "stopped", and the TV played on untimed.
- **Cause:** the receiver reports the replaced media as IDLE/INTERRUPTED, and pychromecast keeps the last contentId it saw, so that status carried our new URL.
- **Fix:** the controller records the media session its load replaced and learns its own session from live statuses; IDLE statuses of other sessions are ignored. The fake device now sends the same merged status. 609 tests.
- **Verified on the Living Room TV:**
  - play episode 5, replace it with episode 4, pick episode 4 again: each ended as "replaced" and played on;
  - time counted without a break (164 → 212 s);
  - no quick "stopped" in the log.

### 6. Deployment (2026-09-28, PRs #7–#11; live on the owner's home server)
The owner swapped steps 6 and 7 so deployment comes before splitting. The owner's plan and runbook are kept with their private infrastructure repository; `docs/installation.md` is the general guide.
- **CI** (`.github/workflows/docker-publish.yml`): tests, then builds `ghcr.io/sandermvanvliet/tellybox` (`sha-…` + `latest`, version `YYYY.MM.DD.<run>`), then deploys over SSH with `docker compose pull && up -d`. The host, port, user, path and key are repository secrets.
- **Image:** runs as uid/gid 1500 (PR #11), an id that is unlikely to clash with host accounts. `/healthz` and the admin dashboard show the version. `tellybox backup DEST --keep N` makes a consistent, integrity-checked copy of the database and prunes old ones.
- **Host setup** (the owner's Ansible role):
  - `/opt/tellybox`, with web and the cast API on non-default ports, because other services on the host already use 8080 and 8081;
  - the admin password from a vault, and backups at 02:30 keeping 14;
  - the firewall allows the web port from the LAN and Tailscale only;
  - an internal DNS name behind nginx, LAN and Tailscale only.
- **Deployed:** CI deploys every merge to `main`.
- **Dev box:** `./data` and `./media` were written by root containers. Running the image locally needs `sudo chown -R 1500:1500 data media` first.

### 7. Playlists and episode titles (2026-09-28, PR #13; show titles PR #15)
Plan: `docs/plans/step7-playlists-titles.md`, approved 2026-09-28 (200-video cap, publish-all included). Built from a shared contract by three parallel subagents (backend, admin, kid app), integrated and reviewed by the controller.
- **CI-7 backend:**
  - `ytdlp.classify_url` recognises video, playlist, both (`watch?v=…&list=…`) and channel URLs. YouTube Mixes (`list=RD…`) count as video.
  - `YtDlp.preview_playlist` makes one `--flat-playlist` call, capped at 200: a real 150-video playlist took 2.2 s. It marks private, deleted, live, upcoming and members-only videos.
  - `ingest.add_playlist` adds a playlist in one transaction: one `source_video` and one DOWNLOAD job per video, skipping videos already in the library.
  - The download job fills in chapters and a missing channel from the full info.
  - Migration 004 records the playlist on each video; `library.publish_held_playlist` publishes only the ready ones.
- **Admin:**
  - The add page asks "Just this video / Whole playlist" for mixed links and refuses channel URLs.
  - The playlist preview has a checkbox per video; already-added and unavailable videos are greyed out. "Hold for approval" is ticked by default. Selected ids are checked against the server-side preview.
  - The library groups held videos per playlist, with "Publish all ready".
- **KA-10 kid app:** a caption under episode thumbnails (episode grid and continue watching), at most two lines, always two lines tall so rows align, and `aria-hidden`. At night only the thumbnail dims, and the caption turns moonlight-coloured (5.1:1 contrast). Screenshots of every sky state at three sizes were reviewed.
- 710 tests.
- **Real-device checks passed** on the owner's home server (2026-09-28): a playlist added with hold and published with "Publish all ready", a `watch?v=…&list=…` link added as just the video, and titles on the phone.
- **Found during the checks:** the Add page hung in Firefox with 6 Tellybox tabs open. Each tab's SSE stream held one of the browser's 6 HTTP/1.1 connections to the site. Fixed by serving the nginx vhost over HTTP/2 (in the owner's infrastructure repository). nginx 1.24 needs `listen … http2`, which then applies to every site on that port. `docs/installation.md` recommends HTTP/2 for the proxy.

### Show titles in the kid app (2026-09-28, PR #15)
- The owner asked for the show name under show tiles too. KA-10 now covers episode and show tiles; the PRD, KA-2, `CLAUDE.md` and `docs/kid-api.md` are updated.
- The show tile's stacked card moved onto a wrapper around the artwork, so it sits behind the picture only. At night the wrapper dims, and the caption turns moonlight-coloured as on episode tiles.
- Checked on the owner's devices after deploy.

### README and installation guide (2026-09-28, PR #14)
- `README.md` with screenshots from a demo instance (invented shows, generated artwork, fake Chromecast), and `docs/installation.md` for people other than the owner.
- Published 2026-09-28 under GPL-3.0, as a fresh repository without the development history. The owner's server details moved to their private infrastructure repository and a git-ignored `CLAUDE.local.md`.

### Interface languages: English, Dutch, German (2026-09-28, NF-13, PR #16)
Plan: `docs/plans/i18n.md`. The foundation was built first, then subagents did the templates and the code in parallel, and a third wrote the translations.
- **Language choice:** `tellybox/i18n.py` picks the language from `Accept-Language` (en, nl, de; English fallback), and a plain ASGI middleware sets it per request. The kid app picks from `navigator.languages`, since it's static.
- **Catalogs:** gettext files in `tellybox/locale/{nl,de}`, read with Babel. There's no compile step. `scripts/i18n.sh` extracts and merges.
- **Admin JS:** runtime text comes from a JSON block (`js_strings.py`) through `admin/static/i18n.js`.
- **Coverage:** 262 strings, translated informally ("je", "du"). `tests/test_i18n.py` fails on anything untranslated, fuzzy or with mismatched placeholders.
- **Also fixed:**
  - the dashboard's player-state badge looked up uppercase states, but the cast service sends lowercase ones, and "loading" had no label;
  - on phones, Retry showed on jobs that hadn't failed, because a component `display` rule overrode `hidden`.
- **Verified after deploy** (PR #16, version 2026.09.28.12): the login page answers in nl, de and en per Accept-Language, both directly and through nginx. The owner checked the translations on the devices: fine.
- **Open:** numbers keep a decimal point in every language ("127.7 KB").
- 743 tests.

## Next

The owner reordered the phases on 2026-09-28 (PRD "Build order after v1"): kid profiles, SponsorBlock, channel subscriptions, manual splitting, smart splitting, then the Tellybox Cast receiver. The decisions from that session are PRD A-7..A-11, SB-1..SB-6 and CR-1..CR-8.

### 8. Kid profiles (v2), deployed; device checks open
"Who's watching" screen, per-profile allowance, usage, continue watching and history, watching together (PR-1..PR-4). There's no PIN, and every profile sees the whole library.
- Plan `docs/plans/step8-profiles.md`, approved 2026-09-29 (decisions A-12..A-15). Subagent briefs: `docs/plans/step8-handoff.md`.
- Contract: migration 005 (`profile.avatar`, `profile.sort_order`), `tellybox/avatars.py`, timer stubs, `docs/cast-api.md`, `docs/kid-api.md`.
- Built by three Sonnet subagents from the contract, merged and reviewed by the controller (2026-09-29). 881 tests.
  - **Timer and cast (A):** watchers; only they accrue time. Per-profile viewing sessions and exhaustion (A-13); a group starts only if every member can (PR-4). Blocking a non-watcher leaves playback alone. Snapshot v2 (a v1 snapshot restores as "everyone"). `play(episode_id, profile_ids)`; autoplay and restart recovery keep the group; group resume position; profiles added or deleted in the admin are picked up live.
  - **Web and admin (B):** `/api/kid/profiles`, `?profiles=` groups (400 `bad_profiles`), play with `profile_ids`, per-profile KidState, `/img/profile/{id}.jpg` (`no-cache`). `/admin/profiles`: add, rename, reorder, avatar picker, photo upload and remove, delete (refused for the last profile, while watching, or while the cast service is unreachable). Dashboard rows per profile with a "watching" badge and an "Everyone" card; history "who" column and `?profile=` filter. nl and de translated.
  - **Kid app (C):** the `#/who` picker (tap to toggle, several kids, go arrow; kids out of time at night and not pickable), eight avatar SVGs, the pick kept per device (day change or 30 min idle asks again, A-12), the corner avatar button, and the sky and time-up reduced for the device's group. With a single profile there is no picker.
  - **Contract notes:** `watching` in the cast state means "in the current episode", not the timer's last group, so a kid who watched earlier can still be deleted. Deleting a profile keeps its watch history rows without a name, and it is the one place where the web service changes position and usage rows (by cascade); the cast service drops the profile on its next tick.
- Timer scenarios checked by hand: A's session max doesn't block B; together with A out of time, the episode finishes and B alone can start again; blocking a non-watcher continues playback.
- Merged as PR #1 (PR numbers restarted in the public repository) and deployed (version 2026.09.29.2). The owner checked the admin pages and the kid app's screens on the devices: fine.
- **Open:** the playback checks on the real TV, in `docs/plans/step8-device-checks.md`. v2 is done when they pass.

### 13. Tellybox receiver (v7), in progress
Moved ahead of steps 9–12 by the owner on 2026-09-29. Plan: `docs/plans/step13-receiver.md` (all of CR-1..CR-8; the spike tries GitHub Pages hosting first, then the home server under a public DNS name). Subagent briefs: `docs/plans/step13-handoff.md`.
- Contract: migration 006 (`settings.receiver_app_id`), `docs/receiver-protocol.md`, device protocol stubs, the cast state's `receiver` block, the Pages workflow, and the spike pages and script (`tellybox/web/receiver/spike*.html`, `scripts/receiver_spike.py`).
- Waiting on the owner: registering the receiver in the Google Cast SDK Developer Console and the Chromecast as a test device, then the spike on the TV.

### Then
9. SponsorBlock (v3) · 10. Channel subscriptions (v4) · 11. Manual splitting (v5) · 12. Smart splitting (v6).

## Open decisions / follow-ups

- Kid app images are cached for an hour (`max-age=3600`), so replaced artwork or thumbnails can take up to an hour to show on kids' devices. The admin images revalidate; consider `no-cache` for the kid app too.
- Show order: the PRD asks only for episode order, so shows keep their creation order; there is no admin control for it.
- The admin test suite is slower (full run ~60 s, was ~25 s), mostly from argon2 hashing in each sign-in; lower the hash cost in tests if it bothers.

- At night the dimmed tiles lose their ink outline against the navy sky (deliberate; revisit if kids find it confusing). The bottom hill is nearly flat on wide desktop screens.
- The kid app has no service worker, so it installs but doesn't work offline (by design: state must be live).
- Deleting an episode also deletes its source video and file once nothing else uses it (v1 episodes share the source MP4).
- `compose.yml` is untested (no compose plugin on the dev box); test it on the server (step 6).
- Measure cold-start latency on the server over wired Ethernet (step 6); add a receiver warm-up only if needed.
- PRD open questions still apply: picture PIN, viewing hours, keep source after split, deploy-flow conventions.
