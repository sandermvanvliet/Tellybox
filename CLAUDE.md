# Tellybox

Self-hosted app that lets young kids pick parent-approved videos on any device and plays them on the TV via a Chromecast, within a daily time allowance. The full spec is in `docs/PRD.md`; it is the source of truth. Requirement IDs (KA-1, WT-3, ES-7, ...) are referenced in commits, tests and code comments.

## Hard rules

- Never use the YouTube app or YouTube receiver on the Chromecast. Episodes are always MP4 files served by our server and cast to the Default Media Receiver. From v7, they go to our own Tellybox receiver instead, with the Default Media Receiver as automatic fallback (CR-6).
- Nothing reaches the kid app without explicit admin approval.
- The kid UI must work without reading: thumbnails, artwork and icons. Short titles on episode and show tiles (KA-10) are an aid for adults only; nothing may depend on them.
- Only playback started by Tellybox is timed or controlled; ignore other casts.
- Nothing is exposed to the public internet. Access is LAN plus Tailscale.

## Environment

- Target: one home server, recent Ubuntu, Docker Compose, CPU only (no GPU).
- Receiver: one original (1st gen) Chromecast, no remote. Media: 720p H.264 + AAC MP4 with faststart.
- Server and Chromecast share a LAN segment. Containers use `network_mode: host` (mDNS discovery).
- Deployment: Docker Compose from the GHCR image; see `docs/installation.md`; releases follow `docs/RELEASING.md`. The owner's own server, network and deploy runbook are in `CLAUDE.local.md`, which is git-ignored and not in the repository. Keep such details out of committed files: no hostnames, IPs, internal domains or SSH ports.

## Stack

- Python 3.12, FastAPI, SQLite (single file), pychromecast, yt-dlp, ffmpeg.
- Smart splitting (v6): OpenCV (headless), our own dHash (numpy + Pillow), ffmpeg `scdet`/`blackdetect` for scene snapping (PySceneDetect dropped: it needs the desktop OpenCV build); Tesseract (eng, nld, deu) for OCR titles, optional at runtime.
- SponsorBlock (v3): through yt-dlp's built-in support (`--sponsorblock-remove`); no separate client.
- Frontend: lightweight, server-rendered or a small SPA; live updates via SSE.
- Interface languages (NF-13): English, Dutch, German via gettext catalogs read with Babel.
- Three services sharing the DB and media volume: `web`, `worker` (jobs), `cast` (Chromecast session + timer). From step 15 they run as one container by default (`python -m tellybox` supervises them), or split by compose.
- yt-dlp must be updatable without rebuilding the image (CI-5).

## How to work

- Build in the order of "Build order after v1" in the PRD (v1 is done): kid profiles, the admin API for Home Assistant (step 9), SponsorBlock, channel subscriptions, manual splitting, Tellybox receiver (step 13, moved ahead), smart splitting. Step 15 (easier installation) was added on 2026-10-02 and is built before 11. One step per branch or PR.
- Before coding a step, propose a short plan and wait for approval.
- Write tests for timer logic (WT-*) with a fake clock and a fake Chromecast; no real device needed for unit tests.
- Releases follow `docs/RELEASING.md`. Never tag, push a tag or publish a release without the owner's explicit go-ahead.
- Keep a running log in `docs/PROGRESS.md`: what's done, what's next, open decisions.
- All interface text is translatable (NF-13); the English text is the message id:
  - Python: `_()`, `ngettext()`, or `N_()` for module constants.
  - Templates: `{{ _("...", name=value) }}`.
  - Admin JS: `t()`/`tn()`, and list the string in `tellybox/web/admin/js_strings.py`.
  - Kid app labels: `tellybox/web/static/i18n.js`.
  - Then run `scripts/i18n.sh` and translate the new entries in `tellybox/locale/{nl,de}`. `tests/test_i18n.py` fails on anything untranslated.
- If a requirement is ambiguous, check the PRD's Assumptions and Open questions, then ask rather than guess.

## Decisions made so far

- Timer: "ignore pauses" counts only playing time, capped by a maximum wall-clock session length (A-2). Allowance resets 04:00. Finish-the-episode grace capped at 15 min.
- A new pick replaces what is playing. Autoplay next episode per show.
- Profiles arrive in v2, but the data model includes them from v1 (single household profile). There is no PIN, and every profile sees the whole library (A-7, A-8).
- SponsorBlock segments are cut out of the file at download, not skipped during playback. The defaults are sponsor, self-promotion and interaction reminders, set globally and overridable per show. A daily re-check for 7 days replaces the file if new segments appear (SB-1..SB-5).
- Channel subscriptions never auto-approve; every upload goes through the inbox (A-9).
- History retained 21 days. Single admin; password login, plus optional OIDC sign-in for one group (AD-6).
- Viewing session (WT-3) spans picks and autoplay; it ends only after 15 min with nothing playing (paused or stopped). BUFFERING counts as playing.
- Reaching the max session length is handled like running out of allowance: finish the episode (grace-capped), then stop.
- "Unlimited today" lifts both the allowance and the max session length. Block and stop-now take effect immediately, without grace.
- Time is not counted during a restart or a lost connection. A lost connection ends the episode after 5 min, or on reconnect if our receiver session is gone.
- Media URLs are HMAC-signed, valid 24 h and stable across restarts; our media is recognised by the `/media/{episode_id}/` path.
- Only the `cast` service writes timer, history and position tables; the web app talks to it via its localhost API.
- The admin API (`docs/admin-api.md`, HA-1..HA-8) uses bearer tokens with `read`/`control` scopes, stored as SHA-256 only; it is independent of the admin password (A-16) and every action goes through the cast service (HA-8).
