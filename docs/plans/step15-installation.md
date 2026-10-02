# Step 15: easier installation (DP-1..DP-8)

Approved by the owner on 2026-10-02. Built before step 11.

## Context

The owner's review "Simplifying Tellybox deployment" lists six ways to make installing Tellybox easier. Today the install guide (`docs/installation.md`) has users clone the repo, run `docker build`, `chown` folders to uid 1500, write a password file, hand-copy a 90-line three-service compose file and set their LAN IP. The owner chose all six options, a setup code in the logs to guard first-run password setup, a small Python supervisor for the single container, and building this **now, before step 11**.

Facts checked while planning:
- `ghcr.io/sandermvanvliet/tellybox:latest` already pulls anonymously (HTTP 200 with an anonymous token), so option 1 needs no visibility change.
- CI (`.github/workflows/docker-publish.yml`) tags only `sha-…` and `latest`, both on every push to main, and builds amd64 only.
- `TELLYBOX_MEDIA_BASE_URL` is already autodetected (`tellybox/config.py`, `lan_ip()`).
- `auth.install_password` (`tellybox/auth.py`) clears the stored hash when no env password is set, so a UI-set password needs a "source" marker.
- The owner's own deployment (a three-service compose file from their deploy-flow repository) sets no `user:`. It keeps working unchanged after these PRs. Only the image tag changes (PR A).

## PRD and docs bookkeeping (in PR A)

- PRD: add an "Installation" requirement table, **DP-1..DP-8** (versioned multi-arch image, release compose and `.env.example`, ownership fixed at start plus PUID/PGID, first-run password with a setup code, single container, install script, HA add-on, platform templates). Also insert build step **15. Easier installation**, "built before 11 by the owner, 2026-10-02". Amend NF-10: compose *or* a single container.
- CLAUDE.md: the Stack line "Three services…" becomes "three services, run as one container by default or split by compose".
- `docs/PROGRESS.md`: a step 15 entry, and "Resume here" updated.

## PR sequence (one branch each)

### A. Versioned, multi-arch image (DP-1)
`docker-publish.yml`:
- Trigger on `push: tags: ['v*']` as well as main.
- `metadata-action` tags:
  - main → `edge` + `sha-…`;
  - `v*` tags → `{{version}}`, `{{major}}.{{minor}}` and `latest`.
- `APP_VERSION` = the tag on tags, the existing date.run number on main.
- `setup-qemu-action`; `platforms: linux/amd64,linux/arm64` on tags (and `workflow_dispatch`); amd64 only on main, to keep the owner's deploys fast.
- The `deploy` job runs only `if: github.ref == 'refs/heads/main'`.
- New `release` job on tags: attach `deploy/docker-compose.yml` and `deploy/.env.example` to the GitHub release (`gh release create/upload`).
- **Owner-side change, same day:** the owner's deploy flow switches its image to `ghcr.io/sandermvanvliet/tellybox:edge`. Otherwise production stays on the last release's `latest`.
- Check the arm64 wheels during the first build (opencv-headless, numpy, argon2-cffi, pillow, `deno` via `yt-dlp[deno]`). The worker's updatable yt-dlp install (`tellybox/ytdlp.py`, pip `--target`) picks the right arch by itself.

### B. No more `chown`: root entrypoint with PUID/PGID (DP-3)
- New `tellybox/entrypoint.py` (`python -m tellybox.entrypoint "$@"`, the Dockerfile `ENTRYPOINT`). Remove `USER tellybox`.
  - Not root (e.g. `user:` set) → `os.execvp` the command unchanged.
  - Root → `PUID`/`PGID` (default 1500). If they differ from the image's, run `usermod`/`groupmod`.
  - For `/data`, `/media` and `/backups` (where present): if the top-level owner differs, `chown -R` once and log it. A matching owner is a no-op, so a large media volume isn't walked on every start.
  - Then `setgroups/setgid/setuid` and `execvp`.
- `tellybox/privileges.py`: `drop_root()`, shared with `cli.main()`. Without it, `docker compose exec … tellybox backup` would now run as root and leave root-owned files in `/data` and `/backups`.
- Decision logic as pure functions, unit-tested; the chown and exec stay a thin shell around them.
- Dockerfile `HEALTHCHECK` via a new `python -m tellybox.healthcheck`. It reads `/proc/1/cmdline`:
  - web or all-in-one → `/healthz` on `TELLYBOX_WEB_PORT`;
  - cast → the cast API `/state`;
  - worker → healthy.
  - The owner's three containers then report sensibly too.

### C. First-run password with a setup code (DP-4)
- Migration `012_admin_setup.sql` adds three `settings` columns:
  - `admin_password_source` (`'env'` | `'ui'`);
  - `admin_setup_code_hash`;
  - `admin_setup_code_created_at`.
- `auth.install_password`:
  - an env password always wins (source `env`);
  - with no env password, a `ui` hash is kept (an `env` one is cleared, as today).
- When no password exists, the web service makes a setup code at start: 8 characters, base32, shown as `XXXX-XXXX`. It stores only the SHA-256 and logs `setup code: … — open /admin/setup`. Each restart makes a new code.
- `GET/POST /admin/setup`: the code, a new password, and the password again (min. 8 characters). It reuses `same_origin`, `throttle_wait_s`/`record_failure` and `create_session`.
- The `locked.html` path redirects to setup instead, and the template text explains where to find the code (`docker compose logs`, or the add-on's Log tab).
- CLI `tellybox reset-password`: clears a `ui` password and ends all sessions; the next web start logs a new code.
- `TELLYBOX_ADMIN_PASSWORD(_FILE)` keep working as before.
- i18n: new strings through `_()`, then `scripts/i18n.sh` and nl/de entries.
- Tests with the fake clock: env vs ui precedence; a wrong code is throttled; a used code is cleared; reset-password.

### D. Single container (DP-5) and the release compose (DP-2)
- New `tellybox/__main__.py` supervisor (`python -m tellybox`):
  - starts `tellybox.web`, `tellybox.cast` and `tellybox.worker` as child processes, with prefixed log lines;
  - forwards SIGTERM/SIGINT;
  - if any child exits, stops the others and exits non-zero, so Docker's restart policy restarts the whole container.
- Dockerfile `CMD ["python", "-m", "tellybox"]`. The split commands stay supported; the owner's compose sets them explicitly.
- `deploy/docker-compose.yml`: one `tellybox` service, `image: ghcr.io/sandermvanvliet/tellybox:${TELLYBOX_TAG:-latest}`, `network_mode: host`, `env_file: .env`, `./data`, `./media` and `./backups` volumes, log rotation.
- `deploy/.env.example`: `TZ`, `TELLYBOX_WEB_PORT`, `TELLYBOX_CAST_API_PORT`, `PUID`/`PGID`, and commented-out `TELLYBOX_MEDIA_BASE_URL` and `TELLYBOX_ADMIN_PASSWORD`.
- Root `compose.yml` (dev) stays as the three-service build.
- Supervisor tests use fake child commands (`python -c`): shutdown fan-out, and one child dying.
- **Docs rewrite** of `docs/installation.md`:
  - get started = `curl` two files → edit `TZ` → `docker compose up -d` → read the setup code;
  - the old three-service file moves to an "Advanced: separate services" section;
  - `TELLYBOX_MEDIA_BASE_URL` becomes an optional override;
  - backups, updating (`docker compose pull`, pinning a version) and troubleshooting are updated, and the `chown` rows removed.
  - README quick start to match.
- **Cut `v0.1.0`** after D merges: the first release with assets and arm64.

### E. Install script (DP-6)
- `deploy/install.sh`, attached to releases:
  - POSIX sh; checks Linux, Docker and the compose plugin;
  - refuses Docker Desktop (`docker info` OperatingSystem);
  - asks for the folder (default `/opt/tellybox`), `TZ` (default from `/etc/timezone`) and the web port;
  - downloads the release compose and `.env`, then runs `docker compose up -d`;
  - waits for `/healthz` and prints the setup code from the logs.
- Checked with `shellcheck` in CI.

### F. Home Assistant add-on (DP-7), new repo `sandermvanvliet/tellybox-ha-addon`
- `repository.yaml` + `tellybox/config.yaml`:
  - `image: ghcr.io/sandermvanvliet/tellybox`, version pinned to the release;
  - `host_network: true`, `arch: [amd64, aarch64]`, `map: [media:rw]`;
  - the add-on's `/data` and `/media` line up with `TELLYBOX_DATA_DIR`/`TELLYBOX_MEDIA_DIR`;
  - `webui: http://[HOST]:[PORT:8080]/admin`;
  - options for the web port and time zone;
  - plus `DOCS.md`.
- The setup code shows in the add-on's Log tab.
- To check in the first build: whether Supervisor accepts a multi-arch manifest without `{arch}` in the image name. If not, CI also pushes `tellybox-amd64`/`tellybox-aarch64` aliases.
- The installation guide links to it.

### G. Platform templates (DP-8)
- In `deploy/`:
  - an Unraid CA template XML (`--net=host`, paths, PUID/PGID);
  - CasaOS and TrueNAS SCALE app entries, both compose-based from the release file;
  - an Umbrel app manifest, if Umbrel allows host networking.
- Synology: a docs section using the release compose in Container Manager.
- The owner submits each to the external catalog (Unraid CA repo plus support thread; TrueNAS, CasaOS and Umbrel app-store PRs). Note these in PROGRESS as owner actions.

## Verification

- Each PR: `pytest -q` (including `tests/test_i18n.py`), and the CI build green.
- A/D: `docker buildx build --platform linux/arm64` locally, or the tag build. Then in a scratch folder, on a fresh Linux VM or the dev box:
  1. `curl` the release files and run `docker compose up -d`;
  2. confirm `data/` and `media/` end up owned by 1500 without any `chown`;
  3. read the setup code, set the password at `/admin/setup`, pick the Chromecast, play an episode;
  4. `docker compose exec tellybox tellybox backup /backups` leaves 1500-owned files.
- B: also `docker run` with `PUID=1000 PGID=1000` and with `--user 1500`.
- C: restart with no env password → new code; set `TELLYBOX_ADMIN_PASSWORD` → it takes over and sessions end; `tellybox reset-password`.
- Owner's production: after the `:edge` switch, the existing three-service deploy keeps running and all three containers become healthy. Real-device check on the owner's Chromecast: pick to playing, time-up and the receiver.
- F: install the add-on on a HA OS test instance (amd64; aarch64 if a Pi is available). Check that it finds the Chromecast and plays.
