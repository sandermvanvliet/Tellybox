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

### Logo and favicon (2026-09-29, branch `brand/logo`)
- **Mark:** a yolk TV whose screen shows the sun going down behind the hill, the kid app's "the sky is the timer" idea. It uses the kid app's colours, with a white-outlined variant for dark backgrounds. The wordmark is Fredoka SemiBold (OFL), outlined to paths.
- **Files:** `docs/images/brand/` (mark, logo, dark variants, `social-preview.png`, sources in `src/`). In the app: `favicon.svg` (the mark simplified for 16 px), `favicon.ico` (16/32/48) and a `/favicon.ico` route, plus the home-screen icon redrawn with the new TV. `scripts/brand.sh` regenerates everything derived.
- **Where:** favicon links in the kid app and admin, the mark in the admin header, the logo at the top of the README (light and dark), and the mark on the headings of the installation guide and the API and protocol docs.
- 1003 tests.

## Next

The owner reordered the phases on 2026-09-28 (PRD "Build order after v1"): kid profiles, SponsorBlock, channel subscriptions, manual splitting, smart splitting, then the Tellybox Cast receiver. The decisions from that session are PRD A-7..A-11, SB-1..SB-6 and CR-1..CR-8. On 2026-09-29 the owner moved the receiver ahead (it keeps step 13) and inserted the admin API for Home Assistant as step 9. The current order is:
- 8: profiles;
- 9: admin API;
- 13: receiver, done;
- 10: SponsorBlock, deployed; device checks open;
- 11: subscriptions;
- 12: manual splitting;
- 14: smart splitting;
- 15: easier installation, added on 2026-10-02 and built before 11.

### Resume here (2026-09-30)

Steps 9 and 13 are done. What's open is on the owner's side:

1. **Step 10, SponsorBlock (v3):** the TV checks in the step 10 entry below.
2. **Step 8, kid profiles (v2):** the playback checks on the real TV, in `docs/plans/step8-device-checks.md`.
3. **PR #10** (installation guide: reboot the Chromecast after changing the receiver app) is open.

On 2026-10-01 the owner chose to build splitting next (steps 12 and 14), ahead of 11, channel subscriptions (v4). Step 12 is done, and step 14 is deployed with its v6 gate open. On 2026-10-02 the owner added step 15, easier installation (DP-1..DP-8), and put it before 11. Step 15 is built and released as v0.1.0; its device checks are open. **The next step to build is 11, channel subscriptions (v4).**

### Release process (issue #30, branch `release-process`)
Releases already existed (v* tags, DP-1); this adds the process around them.
- `docs/RELEASING.md`: versioning, steps, what CI does, checks, fixing a bad release, how others follow releases.
- Tag guard in `docker-publish.yml`: a `v*` tag must equal `version` in `pyproject.toml`, or the test job fails before anything is built.
- `.github/release.yml` groups the generated notes by label; `docs/installation.md` has "Following releases" (Watch, Dependabot example).
- `CLAUDE.md`: no tag or release without the owner's go-ahead.

### 15. Easier installation, built; release and device checks open
Plan `docs/plans/step15-installation.md`, approved 2026-10-02. One PR per part, each built by a Sonnet subagent in a worktree and reviewed by the controller. All merged on 2026-10-02.
- **A. Versioned, multi-arch image (DP-1), #20:**
  - v* tags build amd64 + arm64 as `X.Y.Z`, `X.Y` and `latest`, and create the GitHub release with `docker-compose.yml`, `env.example` and `install.sh`.
  - main builds amd64 only, as `edge` (and `sha-…`), and is the only thing deployed. The owner's deploy flow follows `:edge`.
- **B. Root entrypoint (DP-3), #22:**
  - `python -m tellybox.entrypoint` chowns `/data`, `/media` and `/backups` when their top-level owner differs from `PUID`/`PGID` (default 1500), then drops root.
  - The `tellybox` CLI drops root too, so `exec … tellybox backup` writes service-owned files.
  - Image `HEALTHCHECK` (`tellybox.healthcheck`): reads PID 1's command; web or all-in-one → `/healthz`, cast → `/state`, worker → healthy.
- **C. First-run password (DP-4), #21:**
  - Without a configured password, the web service logs a one-time setup code; `/admin/setup` takes it plus a new password. Migration 012 records where the hash came from (`env` or `ui`); the environment always wins.
  - `tellybox reset-password` clears a browser-set password and prints a new code. The token API (A-16) is unaffected.
- **D. Single container (DP-5) and release compose (DP-2), #24:**
  - `python -m tellybox` supervises web, cast and worker: signals fan out, and one child dying restarts the container. The image's default command.
  - `deploy/docker-compose.yml` + `deploy/.env.example`; the install guide is rewritten around them, with the three-service file under "Advanced: separate services".
- **E. Install script (DP-6), #23:** `deploy/install.sh` (POSIX sh, `curl … | sh`): checks Docker, asks folder/TZ/port, downloads the release files, starts it and prints the setup code; upgrades an existing folder. shellcheck in CI.
- **F. Home Assistant add-on (DP-7), #26:**
  - The entrypoint maps `/data/options.json` (via `TELLYBOX_OPTIONS_FILE`) to env vars and creates missing data/media folders.
  - The add-on itself lives in `sandermvanvliet/tellybox-ha-addon` (image `ghcr.io/sandermvanvliet/tellybox`, version `0.1.0`, host network, `init: false`, data in `/data/tellybox`, media in `/media/tellybox`).
- **G. Platform templates (DP-8), #25:** `deploy/platforms/` for Unraid, TrueNAS SCALE (Install via YAML), CasaOS and Umbrel; Synology in the guide. Checked against each platform's docs, not installed.
- 1442 tests.
- The add-on repository `sandermvanvliet/tellybox-ha-addon` is published (2026-10-02).
- README and installation guide updated for step 15 (2026-10-02).
- **v0.1.0** tagged on 2026-10-02 (145eeeb): the first release. CI run green (test, multi-arch build, release); `0.1.0`, `0.1` and `latest` carry linux/amd64 and linux/arm64, and the release has `docker-compose.yml`, `env.example` and `install.sh`.
- **Open (owner):**
  - Real HA OS checks: the multi-arch pull without `{arch}`, the setup code in the Log tab, the Web UI button (`[PORT:8080]` with host networking), `TZ` inside the container, the Chromecast found and playing.
  - A clean install from the release on a Linux host (`install.sh`, or the two files), then the usual real-device checks.
  - External catalog submissions: Unraid CA, CasaOS AppStore, Umbrel apps (after a release with pinned digests).

### 12. Manual splitting (v5), done
Plan `docs/plans/step12-14-splitting.md` (steps 12 and 14), approved 2026-10-01. Subagent briefs: `docs/plans/step12-handoff.md`. Branch `step12/manual-split`.
- **Decision (owner, 2026-10-01):** the approve form has "delete the original video after cutting", unticked by default; a kept source can be split again (A-21).
- **Contract:**
  - migration 009 (`split_proposal`; job types `split` and `detect`);
  - `tellybox/splitting.py` (segments, chapters on the file timeline, validation);
  - the split storage in `library`;
  - `media_format.cut`, a frame-accurate re-encode. A test checks the first frame of a cut in a clip with a single keyframe.
  - 1205 tests.
- **Found while planning:** `_publish` kept the add-time chapters, which are on the original timeline, over the download's (already shifted by SponsorBlock). Fixed in slice A; `splitting.chapters_on_file` maps older rows.
- Built by three Sonnet subagents in parallel worktrees and merged by the controller (2026-10-01). 1262 tests, including 17 node tests for the plan logic.
  - **Worker (A):**
    - the `split` job: it waits 20 minutes while a part of the source is on the TV or the cast service can't be reached (checked before and after cutting);
    - it re-validates against the probed file, cuts and verifies each kept part, and takes a thumbnail 3 s in;
    - in one transaction: the parts take the old episode's place in the show, hidden when the source is held, the old episodes are deleted (positions by cascade) and, when asked, the source's `file_path` is cleared;
    - SB-6: `request_redownload` raises `SourceSplit`, and split sources get no re-checks;
    - the download's chapters are stored; held downloads list once per source.
  - **Admin (B):** the source video (Range) and frame routes, `GET`/`PUT /admin/api/splits/{id}`, the split page with approve and discard, the episode page's "Split into episodes" / "Part n of m" / "Split again", the SB-6 note, "Splits to review" on the library page, job labels, nl and de.
  - **Editor (C):** `split_plan.js` (pure, node-tested) and `split.js`: step buttons and keys, cut here, use chapters, the parts list (title, keep or leave out, nudges, remove cut), the start-frame strip (ES-7), autosave, the approve check and estimate, progress polling. Phone-first, two columns from 900 px.
- **Merging:** local `main` was behind `origin/main` (PRs #10 and #15), so `origin/main` is merged into the branch. The catalogs conflicted between B and C; both sides' translations are kept.
- **Smoke test** on the dev box: a real web service and worker against a stub cast service (so the real Chromecast was never touched), with a 90 s three-chapter 720p compilation.
  - The page offered the three chapters. The API refused a bad plan (422) and a PUT without Origin (403).
  - Dropping a 2 s intro, renaming and approving with "delete the original" cut 88 s into parts of exactly 28.0, 30.0 and 30.0 s in about 9 s. The parts took the compilation's place between the neighbouring episodes, each with a thumbnail. The untitled one became "Demo compilation (3)", and `file_path` was cleared.
  - Screenshots at 375 and 1280 px were reviewed, with no horizontal scroll.
  - **Found and fixed:** the "delete the original" box came up ticked after an earlier approval with it; it's now unticked every time (A-21).
- Merged as PR #16 and deployed (version 2026.10.01.26).
- **Real-device checks passed (owner, 2026-10-01).** v5 is done. The checks were:
  1. Split a real compilation (with chapters if possible) on the phone: the chapters are offered; move a cut, drop an intro, rename, approve.
  2. Play a part on the TV: it starts cleanly, without frames from the previous episode, and autoplay moves on to the next part.
  3. Approve a split while the compilation is playing: the job waits ("waiting: …" on Jobs) and runs after it stops.
  4. The episode page of a part shows "Part n of m", and the SponsorBlock card shows the SB-6 note instead of the download-again buttons.
- **Open:**
  - The probed fps is the average frame rate (25.011 for a 25 fps clip), so frame steps are off by a hair.
  - The step buttons' "◀ frame"/"frame ▶" wrap onto two lines, which makes them taller than their neighbours.
  - The split job runs on the single worker thread, so a long compilation holds up downloads while it's cut.

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

### 9. Admin API for Home Assistant (v2.1), done
Inserted as step 9 by the owner on 2026-09-29: Phase 0 of the plan "Tellybox × Home Assistant: upstream features & integration plan". It covers API tokens, the admin state and its event stream, override endpoints, the instance id and `/api/info`. The Home Assistant integration itself lives in separate repositories later.
- Plan `docs/plans/step9-ha-api.md`, approved 2026-09-29. Subagent briefs: `docs/plans/step9-handoff.md`. Branch `step9/ha-api`.
- Renumbered: SponsorBlock is now step 10, subscriptions 11, manual splitting 12, and smart splitting 14. The receiver keeps 13, because it's already in progress.
- Contract (2026-09-29): migration 007 (`api_token`, `settings.instance_id`, `override_log.source`), `tellybox/api_tokens.py` with tests, `CastClient.override(kind, value, profile_ids, source)`, `docs/admin-api.md`, `docs/cast-api.md` (`profile_ids`, `source`, `clear`, `timer.next_reset`, `profiles[].session_elapsed_s`), PRD HA-1..HA-8 and A-16..A-18.
- **Cast service (A), merged:** `/overrides` takes `profile_ids` (1–20, `profile_id` still accepted), `source` and the new `clear` kind; unknown ids are a 422 and nothing is applied; `override_log.source`; the state gains `timer.next_reset` (from the timer, no extra DB read) and `profiles[].session_elapsed_s`. 905 tests.
- **JSON API and hub (B), merged:**
  - `TokenGuard` (401 with `WWW-Authenticate`, 403 for the wrong scope);
  - `/api/info`, `/api/admin/state`, `/api/admin/events` (a shared admin hub, the kid hub generalised with `unreachable`, `initial` and `refresh_s`; job and disk figures cached for 10 s);
  - the override routes, including `DELETE /today`;
  - `tellybox/web/overrides.py` (`apply_override`), shared with the dashboard's buttons, which now also refuse more than 240 minutes;
  - `jobs.count_by_status`, `library.count_held_ready`.
- **Admin Integrations page and history (C), merged:**
  - `/admin/integrations`: create (the secret shown once in the POST response, `no-store`), list, revoke;
  - the Home Assistant Cast warning;
  - history shows "via <token>";
  - nl and de.
- **Controller review fixes:**
  - `next_reset` from the timer;
  - a history label for `clear`;
  - the dashboard's 240-minute message translated;
  - integer `allowance_s` and `max_session_s`;
  - the cold-start and unreachable states documented.
- **Smoke test** on a real uvicorn web and cast service, with no Chromecast:
  - `/api/info`;
  - 401 without a token;
  - tokens created on the page;
  - a read-only token gets a 403 on overrides;
  - +15, block, clear;
  - the SSE first event;
  - `override_log.source`;
  - history "via Home Assistant";
  - 401 after revoke;
  - no token in the access log.
- 999 tests.
- Merged as PR #3 and deployed (version 2026.09.29.5). On the server:
  - `/api/info` answers with the instance id, so migration 007 ran;
  - `/api/admin/state` without a token gets a 401.
- **Device checks passed (2026-09-29)** through the Home Assistant integration: tokens, state, +15 moving the sun, "via Home Assistant" in the history, and 401/reauth after a revoke.
- **Home Assistant integration (Phase 1), built and released as v0.1.0 on 2026-09-29, outside this repository.** It lives in two public Apache-2.0 repositories:
  - [`pytellybox`](https://github.com/sandermvanvliet/pytellybox): the async client and a mock Tellybox, 69 tests;
  - [`ha-tellybox`](https://github.com/sandermvanvliet/ha-tellybox): a HACS integration for HA 2026.4 or later, 101 tests, with hassfest and HACS validation green.

  What it has:
  - a Tellybox device: media player, now playing, time left, downloads, time up and last five minutes, and buttons for everyone;
  - one device per kid: time left and used, session, watching, blocked and unlimited, and per-kid buttons;
  - six actions;
  - live updates over `/api/admin/events`;
  - reauth when a token is revoked;
  - nl and de translations.

  It was built by three Sonnet subagents from a contract. The plan and log are in `ha-tellybox/docs/`.
  - pytellybox 0.1.0 is on PyPI. The integration is installed through HACS on the owner's Home Assistant.
  - **The owner's checks all passed (2026-09-29):**
    - the Tellybox device and both kid devices appear;
    - +15 moves the sun, with "via Home Assistant" in the history;
    - play works, and is refused when time is up;
    - revoking the token starts reauth.
  - Phase 1 is done.
- **Deferred from the Home Assistant plan** (owner, 2026-09-29: Phase 0 only):
  - F4 typed events (`time_up`, `last_five`, `override_applied`, `download_ready`…) on the admin stream; today an integration has to diff the state;
  - opt-in zeroconf advertisement (`_tellybox._tcp`);
  - F6: settings `GET`/`PATCH`, daily history, and publishing held downloads from Home Assistant (opt-in).

### 10. SponsorBlock (v3), deployed; device checks open
Sponsor segments are cut out of the file at download through yt-dlp's `--sponsorblock-remove`, with the categories set in Settings and per show, a 7-day daily re-check, and an episode page that lists the removed segments (SB-1..SB-5).
- Plan `docs/plans/step10-sponsorblock.md`, approved 2026-09-30. Subagent briefs: `docs/plans/step10-handoff.md`.
- **Decisions (owner, 2026-09-30):**
  - keyframe cuts by yt-dlp's stream copy, with no re-encode (A-19);
  - a new episode page `/admin/episodes/{id}` for SB-4.
  - Also recorded: videos from before v3 are only cut when the admin downloads them again with SponsorBlock (A-20).
- **What the yt-dlp source (2026.08.19) showed:**
  - the lookup sends only a 4-character hash prefix (SB-1 privacy);
  - an unreachable API fails the whole run before anything is downloaded, so the worker retries without SponsorBlock (SB-5);
  - when a video has no chapters, the cut invents a single one for the whole video, which the wrapper drops.
- **Contract:**
  - migration 008 (the categories on settings and show, the `source_video.sb_*` columns, the `job` table rebuilt for `sb_recheck` and `redownload`, the `position_shift` queue);
  - `tellybox/sponsorblock.py` (categories, segment merging, `remap_position`);
  - in `ytdlp.py`: `sb_categories`, `sponsor_segments()` and `SponsorBlockUnavailable`;
  - `jobs.defer`, `jobs.has_pending_for` and `ingest.request_redownload`.
- Built by three Sonnet subagents and merged by the controller (2026-09-30). 1103 tests.
  - **Worker (A):**
    - the download cuts the effective categories and falls back to uncut on `unreachable`;
    - the daily `sb_recheck` in the 03:00 slot;
    - `redownload` shares the pipeline with the download. It waits 20 minutes while the episode is on the TV or the cast service can't be reached (checked both before and after the download). It replaces the file at the same path in one transaction, with a hardlink backup, then updates the duration and queues `position_shift` rows. The episode keeps its id, title, thumbnail, hidden state and show.
  - **Cast (B):** `store.apply_position_shifts` on each tick and before `play`. Controller review fix: `updated_at` is kept, so continue watching keeps its order.
  - **Admin (C):**
    - the SponsorBlock categories in Settings;
    - "default / off / choose" per show;
    - the episode page (the segments, time removed and status, and "Download again without / with SponsorBlock");
    - job labels;
    - nl and de.
- **Smoke test with the real yt-dlp** (a 185 s video with three sponsor segments):
  - the lookup matches the download's segments;
  - 20.2 s removed; the file probes at 164.9 s, against 164.8 s expected;
  - no invented chapter;
  - with the API unreachable, yt-dlp exits 1 with "Preprocessing: Unable to communicate with SponsorBlock API", which the wrapper detects.
- Merged as PR #6 and deployed (version 2026.09.30.13) on 2026-09-30. The worker log shows migration 008 applied.
- **GHCR, 2026-09-30:** the first deploy failed silently. The server's pull was denied, but the workflow stayed green, so the old version kept running.
  - Cause: the server's registry token had been renewed as a fine-grained token, and GHCR accepts only classic tokens with `read:packages`.
  - Fix: the `tellybox` package is now public. The repository is public too, so the server needs no token.
  - The package is still linked to the archived `Tellybox-private`, which is why its settings page is read-only. To change a setting, unarchive `Tellybox-private` for a moment.
  - Follow-ups:
    - the deploy script now stops on a failed pull (`set -e`);
    - the GHCR login task and `github_pat` in the `tellybox` role in middle-earth-iac can go.
- **Real-device checks (owner, after deploy):**
  1. Add a video with sponsor segments. Its episode page lists them and the time removed. Play it on the TV: the joins are clean, with no sponsor left beyond about two seconds.
  2. Watch a cut episode partway, then press "Download again without SponsorBlock". While it's playing, the job waits. After stopping and redownloading, resume: it continues at the same moment in the video (the earlier cuts are now back in the file).
  3. Turn SponsorBlock off for one show and add a video: its page says it's off, and the file is uncut.
  4. The next morning, the jobs page shows the "Check SponsorBlock" jobs that ran in the 03:00 slot.
- **Open:**
  - the owner's real-device checks above. v3 is done when they pass.
  - A redownload of a split video completes without doing anything (SB-6, step 12 should add a message).
  - The admin API's job counts (HA-2) include the daily re-check jobs for a moment.

### 14. Smart splitting (v6), deployed; v6 gate open
Plan: the step 14 part of `docs/plans/step12-14-splitting.md`. Briefs: `docs/plans/step14-handoff.md`. Branch `step14/smart-split`, merged as PR #17 on 2026-10-01, after the step 12 TV checks passed.
- **Deployed** as version 2026.10.01.27. CI ran 1346 tests with none skipped, so the OCR tests ran with Tesseract installed.
- **Image size:** 294 → 467 MB compressed (amd64), from OpenCV, Tesseract with three languages, and imagehash's scipy and PyWavelets. Only dHash is used, so imagehash was replaced by our own bit-identical dHash (`detect.dhash`, guarded by `tests/test_dhash.py`).
- **Decisions (owner, 2026-10-01):**
  - A-22: a compilation picked up by automatic detection stays hidden until its split is approved or it's published whole.
  - No PySceneDetect: version 0.7 requires the desktop OpenCV build. Scene changes come from ffmpeg `scdet`, and black frames from `blackdetect`. The CLAUDE.md stack line and the PRD are updated.
  - Tesseract with English, Dutch and German in the image. CI installs it so the OCR test runs there; on the dev box it skips without the `tesseract` binary.
- **Contract:**
  - migration 010 (`split_profile`, `split_reference`, `split_proposal.detected_json`, `source_video.awaiting_split`);
  - `tellybox/detect` types and hashing;
  - the profile storage in `library`;
  - `tests/detect_clips.py` (synthetic compilations with title cards and black at known times).
- **Measured while writing the contract:**
  - pHash was unstable on mostly flat logo crops: an identical card scored 10–14 bits from itself.
  - Using one scaler for both sides also matters: every frame, sampled or marked, goes through the same ffmpeg filter (`SAMPLE_FILTER`).
  - With dHash: the same card 0–1, a heavily degraded one 9–12, anything else 22 or more. So the default threshold is 6, and an ES-5 re-scan accepts threshold + 8.
- Built by three Sonnet subagents and merged by the controller. 1344 tests, plus a `slow` benchmark run on request. 1Password commit signing was locked for part of the run, so A and B were applied as patches and committed together with C.
  - **Engine (A):**
    - 2 fps sampling through one ffmpeg pipe;
    - runs of matching frames become one hit;
    - the length hint drops hits that are too close and re-scans gaps at 5 fps with the looser threshold, at half the confidence;
    - snapping to the black start (preferred) or a scene change, up to the snap window before the card;
    - OCR with Otsu thresholding, inverted for light-on-dark text.
    - **Controller fix:** a snap window ends at the previous card, so a short episode can't snap back past it (regression test).
    - **NF-6:** a 30-minute 720p compilation is detected in 79 s (4.4 % of its length).
  - **Worker (B):**
    - the `detect` job writes a `review` proposal (OCR titles, a `detected_json` entry per cut); cuts that would leave a part under 5 s are merged away;
    - ES-10: after a new download in a show with auto-detect, a video longer than 1.5× the hint (20 minutes without one) is published hidden with `awaiting_split`, and detection is queued;
    - approving its split makes the parts visible and clears the flag (A-22).
  - **Admin (C):**
    - the split page: "Mark as title card" (freeze the frame, drag a region or take the whole frame; touch works), the title cards, "Find cuts" with progress, "sure / check / unsure" and snap-kind badges, and "Publish as one video" while held;
    - the show page's "Splitting" card (references, threshold, episode length in minutes or mm:ss, snap window, OCR and its region in percent, auto-detect);
    - "Detected" and "Hidden until approved" badges on the library page;
    - nl and de.
- **Smoke test** on the dev box: a real web service and worker against a stub cast service, with a synthetic 3-minute compilation: an 8 s intro and four episodes, one with a degraded card.
  - Detection was refused before any card was marked (409).
  - Marking the card and setting a 0:42 episode length, then "Find cuts", gave a proposal in about 6 s. All four cuts landed exactly on the black before each card. The degraded one was found by the re-scan and marked "unsure" (0.37 against 0.93).
  - Leaving out the intro and approving cut four visible parts (42.6, 47.6, 40.6 and 44.6 s: black, card and episode each).
  - Screenshots of the split page at 375 px and the show page at 1280 px were reviewed.
- **OCR with Tesseract installed** (dev box, 2026-10-01): the matched logo region rarely holds the title, so it read nothing. OCR now reads the whole frame unless a title region is set, and every title on the test cards was read.
- **Found while making the README screenshots** (2026-10-01, an invented show with Fredoka title cards):
  - Whole-frame OCR read the logo and scenery too ("u The Big Splash", "7 en Croak"). Without a title region it now takes Tesseract's sparse words, groups them into lines and keeps the line with the tallest confident words. All four titles were then read exactly, and a test covers it.
  - The review strip showed black frames for detected cuts, because they snap to the black before the card. A detected cut's strip frame is now its title card; manual cuts keep their exact start frame.
- **Owner's local check** (2026-10-01, a local web service and worker with a stub cast service, `.local-test/`, git-ignored): detection on real videos "working well enough".
- **Open:**
  - the owner's v6 gate on the deployed version: detection accepted on two real shows;
  - an ES-10 check on the server: a new compilation in a show with auto-detect stays hidden, appears in "Splits to review", and its parts appear once approved;
  - an auto-detected compilation waits hidden in review, then plays split on the TV;
  - OCR on real title cards;
  - the image size growth from OpenCV and Tesseract, to measure on the first CI build;
  - a failed detect job leaves an auto-held compilation hidden until "Publish as one video" (by design);
  - no dashboard count for proposals to review (optional, skipped).

### Receiver resilience (CR-6 hardening), deployed; device checks open
Plan: `docs/plans/receiver-resilience.md` (approved 2026-10-01). Branch `receiver/resilience`. It follows the 2026-10-01 incident, where the TV sat on the Default Media Receiver from the start of a pick and the reason was lost with the container logs.
- **Event log:** migration 011 (`receiver_event`), written by the cast service through `store.record_receiver_event` and purged by the worker after 21 days. Kinds: `launch_ok`, `launch_failed`, `refused`, `fallback`, `lost`, `recovered`, `recover_failed`, `page_error`. The cast state's `receiver` block gains `failures_24h`, `launches_24h`, `last_failure` and `refused` (`docs/cast-api.md`).
- **Retry:** a failed launch quits the half-started app, waits 1 s and tries again with 15 s instead of 8 s. A refusal (an immediate `RequestFailed`, or the launch error `CANCELLED`) isn't retried.
- **Fallback with backoff:** none after the first failed pick, then 5, 15 and 30 min; a refusal is 30 min at once; any successful launch resets it. The autoplay boundary after a one-off fallback goes back to our receiver.
- **Mid-episode recovery:** our app vanishing (no app or Backdrop, with no other app within 10 s) relaunches the episode once, at the estimated position, in the same watch session. Another app is still a take-over (WT-9). No recovery when time is up, after our own stop, in the night hold, after a reconnect, or a second time.
- **Receiver page:** `sdkload.js` retries the Cast SDK up to 3 times (2 s, 4 s); `hello` gains `sdk_attempts` and `load_ms` (additive, `docs/receiver-protocol.md`). The page change goes live with the next Pages publish.
- **Dashboard:** the TV receiver line shows the last problem with its time, and a hint to restart the Chromecast after a refusal. nl and de.
- **Tests:** 1385 pass. The fake device gained slow and refused launches, a half-started app, `quit_app` and `vanish()`; `CastController` takes an injectable `sleep`.
- Built by one Sonnet subagent, from the plan, and reviewed by the controller.
- **Review fix:** the subagent's recovery waited only 3 s for another app before relaunching ours. On the 1st gen, YouTube's cold start can take longer than that, and relaunching ours would cut that cast off (WT-9). The wait is now 10 s, with a test for another app arriving 8 s after ours vanished.
- Merged as PR #19 and deployed (version 2026.10.01.30) on 2026-10-01; Pages republished the receiver page.
- **Device checks (owner):**
  1. A normal pick still starts on the Tellybox receiver, cold and warm.
  2. Forced fallback: set a wrong receiver app ID in Settings, pick (Default Media Receiver; the dashboard shows the problem), restore the ID, pick again (back on the Tellybox receiver, no 30-minute wait).
  3. Mid-episode: stop the receiver app from another phone's Google Home, or reboot it, during an episode. The episode comes back at the same spot.

### 13. Tellybox receiver (v7), done
Moved ahead of SponsorBlock, subscriptions and splitting by the owner on 2026-09-29; it keeps number 13. Plan: `docs/plans/step13-receiver.md` (all of CR-1..CR-8; the spike tries GitHub Pages hosting first, then the home server under a public DNS name). Subagent briefs: `docs/plans/step13-handoff.md`.
- Contract: migration 006 (`settings.receiver_app_id`), `docs/receiver-protocol.md`, device protocol stubs, the cast state's `receiver` block, the Pages workflow, and the spike pages and script (`tellybox/web/receiver/spike*.html`, `scripts/receiver_spike.py`).
- Contract merged as PR #2; GitHub Pages publishes `tellybox/web/receiver/` at `…/receiver/`.
- Built by three Sonnet subagents and merged on `step13/receiver` (2026-09-29). 946 tests.
  - **Cast service (A):**
    - the receiver launch with `ReceiverUnavailable`;
    - the `urn:x-cast:tellybox` controller;
    - WT-9 with both app ids;
    - the fallback to the Default Media Receiver for 30 minutes;
    - `state` pushes (throttled, and on `hello`), with `loading` before each load and `up_next` only when autoplay will continue;
    - the 10-minute night hold through a new `stop_media()`;
    - stats logging, and the `receiver` block in the state.
  - **Receiver page (B):**
    - `index.html`, `receiver.css`, `ui.js` (pure) and `cast.js` (the CAF glue, our own `<video>` through `setMediaElement`), plus `dev.html`;
    - old-Chrome-safe code;
    - static gradients;
    - no animation over the video.
  - **Web and admin (C):**
    - `/receiver/` served with `no-cache`;
    - the app ID setting;
    - the dashboard's "TV receiver" line;
    - nl and de;
    - an installation guide section.
- `main` merged into the branch after step 9 (PR #3). The conflicts were in `app.py` (both sides' constants kept) and the nl and de catalogs (both sides' translations kept, then re-extracted). 1064 tests pass.
- **Controller fix:** with no app id (or during a fallback), `play` now moves any other running media app, such as the Tellybox receiver after its app id was cleared, to the Default Media Receiver first. Before, our media would have loaded into it and counted as taken over.
- TV screenshots from `receiver/dev.html` were shown to the owner (private artifact page).
- **Cast console (2026-09-30):** the owner registered an unpublished Custom Receiver and added the Chromecast as a test device. The app ID is kept out of the repository; it goes into admin Settings. The console URL is `https://sandermvanvliet.github.io/Tellybox/receiver/index.html`.
- **Spike passed (2026-09-30):** S1..S9 all pass on the 1st gen, with findings in `docs/spike-receiver.md`.
  - GitHub Pages hosting stays, and media and images over plain HTTP from the LAN load from the HTTPS page.
  - CAF 3.0.0156 runs on Chrome 90. Every CAF call in `cast.js` exists there (checked through remote DevTools on port 9222).
  - Tap-to-playing is ~4.3 s cold and 0.7 s warm, so no warm-up is needed. The overlay drops no frames.
  - A wrong app id fails with `RequestFailed` in 0.02 s, which already becomes `ReceiverUnavailable`. The launch timeout stays at 8 s.
- The seams needed no code changes. The spike pages and script are removed, the installation guide notes HTTP media, and `main` (step 10) is merged into the branch. 1168 tests pass.
- Merged as PR #8 and deployed (CI run 16). Pages publishes the receiver from `main`.
- **Favicon (PR #9):** a copy of `favicon.svg` next to `index.html` and `dev.html`, refreshed by `scripts/brand.sh`. A test keeps the two identical.
- **Rollout (2026-09-30):** after the console URL moved from `spike.html` to `index.html`, every launch failed with `CANCELLED` until the Chromecast was rebooted, because it keeps an app's settings until it restarts. The cast service fell back to the Default Media Receiver as designed. PR #10 adds this to the installation guide.
- **Real-device checks passed (owner, 2026-09-30):** loading with artwork and the corner sky; no stutter (0 dropped frames in every stats line); up next, then autoplay; the sky sinking to dusk; time up, then the night scene; no sky on an unlimited day. The fallback (#6) was seen during the rollout, and ignoring other apps (#8) is unchanged since v1.
- **Found in the checks: the night hold didn't survive a restart.** A deploy at 13:06 restarted the cast service during the night screen. The deadline lived only in memory, and the page disables the TV's idle timeout, so the receiver stayed up. Fixed in PR #14: an idle receiver with no hold gets a fresh 10-minute one.
- **Stale admin pages after deploys:** `/static` and `/admin/static` had no `Cache-Control`, so browsers cached the scripts and styles heuristically until a forced refresh. Fixed in PR #13: both now use `no-cache` and revalidate with their ETag.
- **Docs:** the README and the installation guide cover SponsorBlock and the receiver (PR #11), and the README has TV screenshots rendered from `dev.html` (PR #12).
- **Receiver published (2026-09-30):** the owner published the Cast app (app ID `55AAC641`, receiver on GitHub Pages from `main`), so it runs on any Chromecast without serial registration. CR-1 and the installation guide now tell self-hosters to use that app ID; registering an own app is only for forks that change the receiver. Every receiver change on `main` now reaches other households' TVs, so keep the receiver protocol backward compatible with older servers.
- **Open follow-ups from the subagents:**
  - The receiver shows the loading layer from LOAD_START even before a `loading` message (documented in the protocol). The corner sky is drawn at night while a last episode finishes after time's up.
  - A parent stop-now keeps the night screen only when the day is already out of time; otherwise it quits as in v1.
  - `_sky()` in the controller runs one small profile query per tick while the receiver runs; cache it if it shows up.
  - No lint was run (ruff isn't in the venv).

### Then
11. Channel subscriptions (v4) · 12. Manual splitting (v5) · 14. Smart splitting (v6).

## Open decisions / follow-ups

- Branding: upload `docs/images/brand/social-preview.png` as the repository's social preview (GitHub settings, by hand).
- Kid app images are cached for an hour (`max-age=3600`), so replaced artwork or thumbnails can take up to an hour to show on kids' devices. The admin images revalidate; consider `no-cache` for the kid app too.
- Show order: the PRD asks only for episode order, so shows keep their creation order; there is no admin control for it.
- The admin test suite is slower (full run ~60 s, was ~25 s), mostly from argon2 hashing in each sign-in; lower the hash cost in tests if it bothers.

- At night the dimmed tiles lose their ink outline against the navy sky (deliberate; revisit if kids find it confusing). The bottom hill is nearly flat on wide desktop screens.
- The kid app has no service worker, so it installs but doesn't work offline (by design: state must be live).
- Deleting an episode also deletes its source video and file once nothing else uses it (v1 episodes share the source MP4).
- `compose.yml` is untested (no compose plugin on the dev box); test it on the server (step 6).
- Measure cold-start latency on the server over wired Ethernet (step 6); add a receiver warm-up only if needed.
- Admin API (step 9) notes:
  - The SSE tests in `tests/web/api/` reach into FastAPI's router internals (`original_router.routes`, `body_iterator`), because `TestClient` buffers streams. They need updating if FastAPI changes these.
  - The admin hub's job and disk refresh runs synchronously on the event loop every 10 s, as the kid hub's DB reads do. Move it to a thread if the media disk walk gets slow.
  - Open questions from the Home Assistant plan:
    - Should Home Assistant eventually go into HA core, or stay HACS-only?
    - Should per-kid profiles be sub-devices in Home Assistant?
- PRD open questions still apply: picture PIN, viewing hours, keep source after split, deploy-flow conventions.
