# <picture><source media="(prefers-color-scheme: dark)" srcset="images/brand/mark-dark.svg"><img src="images/brand/mark.svg" alt="" width="36" align="top"></picture> Installing and deploying Tellybox

This guide takes you from an empty server to kids picking videos on the TV. Setup takes 15 to 30 minutes. Then it covers HTTPS, remote access, backups, updates and troubleshooting.

- [What you need](#what-you-need)
- [Quick install script](#quick-install-script)
- [1. Download the two files](#1-download-the-two-files)
- [2. Edit the settings](#2-edit-the-settings)
- [3. Start it](#3-start-it)
- [4. First-run setup](#4-first-run-setup)
- [Other platforms](#other-platforms)
- [Configuration reference](#configuration-reference)
- [Tellybox receiver (optional)](#tellybox-receiver-optional)
- [HTTPS with a reverse proxy](#https-with-a-reverse-proxy)
- [Sign in with OIDC (optional)](#sign-in-with-oidc-optional)
- [Remote access with Tailscale](#remote-access-with-tailscale)
- [Backups and restoring](#backups-and-restoring)
- [Updating](#updating)
- [Advanced: separate services](#advanced-separate-services)
- [Troubleshooting](#troubleshooting)
- [Command-line tools](#command-line-tools)

## What you need

| | |
| --- | --- |
| **Server** | A Linux machine that stays on, with Docker Engine and the Compose plugin (a home server, NAS, mini PC or Raspberry Pi). The image is built for amd64 and arm64. Tellybox is developed on Ubuntu and Fedora. No GPU is needed. |
| **Chromecast** | Any Chromecast or Google TV that plays through the standard Cast "Default Media Receiver". Even the original 2013 Chromecast works. The optional [Tellybox receiver](#tellybox-receiver-optional) also runs on the original, with nothing to register. |
| **Network** | The server and the Chromecast on the **same LAN segment**. Tellybox finds the Chromecast with mDNS, and the Chromecast downloads the video straight from the server. |
| **Internet** | Outbound only: the server fetches videos from YouTube and segment data from the SponsorBlock API. With the Tellybox receiver, the Chromecast also loads the receiver page from GitHub Pages. Nothing comes in from the internet. |
| **Disk** | About 0.5 to 1 GB per hour of video (720p H.264). |
| **Ports** | One TCP port for the web app (8080 by default), reachable from your LAN, including the Chromecast. A second port (8081 by default) is used on localhost only. |

> **Docker Desktop on macOS or Windows doesn't work.** Tellybox needs `network_mode: host` for mDNS discovery, and Docker Desktop runs containers in a VM, where host networking can't reach your LAN. Use a Linux host.

## Quick install script

On a Linux host with Docker Engine and the Compose plugin, one line does the setup:

```sh
curl -fsSL https://github.com/sandermvanvliet/Tellybox/releases/latest/download/install.sh | sh
```

The script:

1. Checks for Linux, Docker and the Compose plugin, and refuses Docker Desktop.
2. Asks for the folder (default `/opt/tellybox`), the time zone and the web port. It uses `sudo` only if the folder isn't writable.
3. Downloads the release's `docker-compose.yml` and `.env`, and fills in the time zone and port.
4. Starts Tellybox with `docker compose up -d` and waits until it answers.
5. Prints the admin setup address and code, and the address for the kids.

Run it again on an existing folder to upgrade. It keeps your `.env`, offers to refresh `docker-compose.yml`, and pulls the new image.

To skip the questions, pass `--yes` and set what you want in variables: `TELLYBOX_DIR`, `TZ`, `TELLYBOX_WEB_PORT`, and `TELLYBOX_VERSION` for a specific release such as `v0.1.0`:

```sh
curl -fsSL https://github.com/sandermvanvliet/Tellybox/releases/latest/download/install.sh | TELLYBOX_DIR=$HOME/tellybox TZ=Europe/Amsterdam sh -s -- --yes
```

Steps 1 to 3 below do the same by hand.

## 1. Download the two files

Pick a home for Tellybox; this guide uses `/opt/tellybox`. You need two files from the latest release: the compose file and a settings file.

```sh
sudo mkdir -p /opt/tellybox && cd /opt/tellybox
sudo curl -fsSLO https://github.com/sandermvanvliet/Tellybox/releases/latest/download/docker-compose.yml
sudo curl -fsSL https://github.com/sandermvanvliet/Tellybox/releases/latest/download/env.example -o .env
```

Tellybox is one image on the GitHub Container Registry, for amd64 and arm64. It runs the three services (web app, Chromecast control and background jobs) in **one container**.

## 2. Edit the settings

Open `.env` and set your time zone in `TZ`. The daily reset and the history use it. If port 8080 or 8081 is already taken on the server, change `TELLYBOX_WEB_PORT` or `TELLYBOX_CAST_API_PORT` too (UniFi uses both, for example). Everything else can stay as it is.

The folders are created for you. The container runs as **uid/gid 1500** by default (set `PUID` and `PGID` in `.env` to pick another owner). It starts as root only to take ownership of `data/`, `media/` and `backups/`, then drops privileges, so you don't need to `mkdir` or `chown` anything:

| Folder | Contents |
| --- | --- |
| `data/` | The SQLite database, the media-link signing key and the self-updating yt-dlp install |
| `media/` | Episodes, thumbnails and show artwork |
| `backups/` | Database backups (see [Backups](#backups-and-restoring)) |

**The admin password is optional here.** Without one, Tellybox asks you to choose it in the browser on first run, using a setup code from the logs (see [First-run setup](#4-first-run-setup)). If you prefer to set it from the host, for example to manage it with your own secrets tooling, uncomment `TELLYBOX_ADMIN_PASSWORD` in `.env` (or use `TELLYBOX_ADMIN_PASSWORD_FILE`, see the [configuration reference](#configuration-reference)).

Some notes:
- **On Fedora or RHEL with SELinux**, add `:z` to each bind mount in the compose file (`./data:/data:z`).
- **Run only one Tellybox per Chromecast.** Two cast services will fight over the same TV.
- The compose file uses `network_mode: host`. That's required: Tellybox finds the Chromecast with mDNS, and the Chromecast downloads the video straight from the server.

## 3. Start it

```sh
cd /opt/tellybox
docker compose up -d
docker compose ps                      # should become "healthy" within a minute
docker compose logs -f tellybox        # watch it find your Chromecast
```

The database is created and migrated on first start. The worker installs its own updatable copy of yt-dlp into `data/tools/`, which takes a few seconds.

### Firewall

If the server runs a firewall, allow the web port from your LAN only. The Chromecast needs to reach it. With `ufw`:

```sh
sudo ufw allow from 192.168.1.0/24 to any port 8080 proto tcp
```

The cast API (8081) listens on `127.0.0.1` only, so it needs no rule. mDNS discovery works through outgoing multicast; with the default ufw policy (outgoing allowed) nothing else is needed.

**Don't forward any port from your router.** Tellybox has no internet-facing features, and the kid app has no login by design. Anyone who can reach it can play approved videos.

## 4. First-run setup

1. **Choose the admin password:** find the one-time setup code in the log, then open `http://<server>:8080/admin/setup`:

   ```sh
   docker compose logs tellybox | grep "setup code"
   ```

   (In Home Assistant, it's in the add-on's Log tab.) Enter the code, then choose a password of at least 8 characters, and you're signed in. The code stops working once used, and a restart makes a new one. If you configured a password in `.env` instead, just sign in at `http://<server>:8080/admin`.
2. **Choose the TV:** in **Settings**, pick your Chromecast. With only one on the network, it's chosen automatically. Tellybox remembers it and reconnects on its own after restarts or power cuts.
3. **Set the limits:** also in **Settings**:
   - the daily allowance (default 60 minutes);
   - how time is counted (only playing time, or wall clock);
   - the longest viewing session (default 90 minutes);
   - the finishing grace (default 15 minutes);
   - the reset time (default 04:00);
   - the SponsorBlock segments to cut (default: sponsors, unpaid or self promotion, and interaction reminders). Tick nothing to turn it off. Each show can use the default, turn it off, or choose its own under **Library**.
4. **Add something:** in **Add**, paste a YouTube video or playlist link. Check the preview and add it. Downloads show up under **Jobs**. Anything added with "hold" appears under **Library → Held downloads** until you publish it.
5. **Give the kids the app:** open `http://<server>:8080` on their tablet or phone and use "Add to Home Screen". It opens full screen like an app.
6. **Try it:** tap an episode. The TV should start within a few seconds.
7. **Optionally, set up the Tellybox receiver** for the sky, loading and goodnight screens on the TV: see [Tellybox receiver](#tellybox-receiver-optional).

## Other platforms

The steps above work on any Linux host. If you run a NAS or a home-server system, these shortcuts may suit you better. They all run the same image with host networking. Ready-made files are in [`deploy/platforms/`](../deploy/platforms/). After the install, continue with [First-run setup](#4-first-run-setup).

- **Home Assistant OS:** use the [Tellybox add-on](https://github.com/sandermvanvliet/tellybox-ha-addon). In **Settings > Add-ons > Add-on store**, open the menu, choose **Repositories** and add `https://github.com/sandermvanvliet/tellybox-ha-addon`. Install **Tellybox**, start it, and read the setup code in its **Log** tab. Videos go to `/media/tellybox`, so they show up in Home Assistant's media browser. The [Tellybox integration](https://github.com/sandermvanvliet/ha-tellybox) adds the dashboard controls.
- **Unraid:** copy `deploy/platforms/unraid/tellybox.xml` to `/boot/config/plugins/dockerMan/templates-user/` on the flash drive. Then choose **Docker > Add Container** and pick the Tellybox template. It runs as user 99 and group 100, the Unraid convention, and the media folder defaults to `/mnt/user/media/tellybox`. Open the WebUI button for `/admin`.
- **TrueNAS SCALE** (24.10 or newer): create a dataset with `data`, `media` and `backups` folders. Open `deploy/platforms/truenas/docker-compose.yml`, change `tank` to your pool name and set `TZ`. Then choose **Apps > Discover Apps > menu > Install via YAML**, name it `tellybox` and paste the file. Sign in with OIDC isn't supported on this platform yet: its file doesn't pass the `TELLYBOX_OIDC_*` settings.
- **CasaOS:** choose **App Store > Custom Install**, paste `deploy/platforms/casaos/docker-compose.yml` and install. Data lives under `/DATA/AppData/tellybox/`. Set `TZ` first. Sign in with OIDC isn't supported on this platform yet: its file doesn't pass the `TELLYBOX_OIDC_*` settings.
- **Umbrel:** not available yet. The app files are ready in `deploy/platforms/umbrel/`, but the app only appears in a community app store once someone publishes it there. Sign in with OIDC isn't supported in these files yet.
- **Synology Container Manager:** you need DSM 7.2 or newer. Host networking works there. Open **Container Manager > Project > Create**, set the path to `/volume1/docker/tellybox` and choose **Create docker-compose.yml**. Paste the release `docker-compose.yml` from step 1, and replace `env_file: .env` with an `environment:` list holding the values from `env.example` (at least `TZ`). Then build the project.

## Configuration reference

All settings are environment variables. What you can change in the admin pages (allowances, timer rules, the Chromecast) lives in the database instead.

| Variable | Default | Meaning |
| --- | --- | --- |
| `PUID`, `PGID` | `1500`, `1500` | The uid/gid the services run as. The container starts as root, takes ownership of `/data`, `/media` and `/backups` (top-level check only, so a large library isn't scanned on every start), then drops to this user. `0` means run as root and must be set explicitly. Ignored when you start the container with `user:` or `--user`. |
| `TELLYBOX_TAG` | `latest` | Which image version the release compose file runs: `latest` (the newest release), a version such as `0.1` or `0.1.0`, or `edge` (the main branch). Read by Compose, not by Tellybox. See [Updating](#updating). |
| `TELLYBOX_ADMIN_PASSWORD_FILE` | none | A file containing the admin password (preferred over the variable below). Overrides a password chosen in the browser. |
| `TELLYBOX_ADMIN_PASSWORD` | none | The admin password itself. Overrides a password chosen in the browser. If neither is set, you choose the password in the browser on first run. Changing the password signs out every session. |
| `TELLYBOX_OIDC_ISSUER` | none | Turns on [sign-in with OIDC](#sign-in-with-oidc-optional): the provider's issuer URL, e.g. `https://id.example.org`. Then the client id, redirect URI and admin group are required too, or Tellybox doesn't start. |
| `TELLYBOX_OIDC_CLIENT_ID` | none | The client id from the provider. |
| `TELLYBOX_OIDC_CLIENT_SECRET_FILE` | none | A file containing the client secret (preferred over the variable below). Leave both unset for a public client; PKCE is always used. |
| `TELLYBOX_OIDC_CLIENT_SECRET` | none | The client secret itself. |
| `TELLYBOX_OIDC_REDIRECT_URI` | none | Your Tellybox address with `/admin/oidc/callback`, e.g. `https://tellybox.example.org/admin/oidc/callback`, exactly as registered at the provider. |
| `TELLYBOX_OIDC_ADMIN_GROUP` | none | Only members of this group may use the admin pages with OIDC. |
| `TELLYBOX_OIDC_GROUPS_CLAIM` | `groups` | The claim holding the user's groups (a list, or one name). |
| `TELLYBOX_OIDC_SCOPES` | `openid profile email groups` | Scopes to request. Must include `openid`. Some providers need a different scope for the groups claim. |
| `TELLYBOX_WEB_PORT` | `8080` | Port for the kid app, the admin pages and the media files. |
| `TELLYBOX_WEB_HOST` | `0.0.0.0` | Address the web app listens on. |
| `TELLYBOX_MEDIA_BASE_URL` | `http://<LAN IP>:<web port>` | Optional override. The URL the Chromecast uses to fetch videos is detected automatically. Set it explicitly if the server has several interfaces (Docker bridges, VPNs) or the detection picks the wrong one. Use plain `http://` and an IP address: a Chromecast can't resolve local-only DNS names. |
| `TELLYBOX_CAST_API_PORT` | `8081` | Internal API of the cast service. |
| `TELLYBOX_CAST_API_HOST` | `127.0.0.1` | Keep this on localhost. |
| `TELLYBOX_DATA_DIR` | `/data` (in the image) | Database, signing key and yt-dlp install. |
| `TELLYBOX_MEDIA_DIR` | `/media` (in the image) | Video files and images. |
| `TELLYBOX_DB` | `<data>/tellybox.db` | Database path. |
| `TELLYBOX_SECRET_FILE` | `<data>/secret.key` | Key for signing media links. It's created on first start. Media links stay valid across restarts for 24 hours. |
| `TELLYBOX_OPTIONS_FILE` | none | A JSON file with `web_port`, `cast_api_port`, `media_base_url`, `admin_password` and the OIDC options (`oidc_issuer`, `oidc_client_id`, `oidc_client_secret`, `oidc_redirect_uri`, `oidc_admin_group`), read at start-up. Used by the Home Assistant add-on (`/data/options.json`). Variables you set yourself win over the file. When the container starts as root, the data and media folders are also created if missing. |
| `TZ` or `TELLYBOX_TZ` | the host's zone, else UTC | Local time zone for the daily reset, schedules and history. |

## Tellybox receiver (optional)

By default, episodes play on the Chromecast's Default Media Receiver, which works out of the box. The Tellybox receiver is an optional Cast app of your own that adds the sky clock, a "loading" screen, an "up next" card and a calm night screen on the TV. It shows no text, and it plays the same MP4 files from your server. If you skip this section, nothing changes.

The Tellybox receiver is a published Cast app, so it runs on any Chromecast without registering anything with Google:

1. **Enter the application ID** `55AAC641` in Tellybox under **Settings → Tellybox receiver app ID** and save. The cast service picks it up within 15 seconds. Leave it empty to go back to the Default Media Receiver.
2. **Try it:** start an episode. The **TV receiver** line on the dashboard should read "Tellybox receiver".

The receiver page is hosted on this repository's GitHub Pages (`https://sandermvanvliet.github.io/Tellybox/receiver/`). It holds no household data: the cast service sends it the timer state, and the videos and images still come from your server over your LAN, on plain HTTP if you like. The page follows the latest version of Tellybox, so keep your server up to date (see [Updating](#updating)).

The receiver protocol and the states it shows are described in `docs/receiver-protocol.md`.

### If the receiver doesn't start

Tellybox falls back on its own: if the Tellybox receiver can't be launched (page unreachable, or a timeout), the same episode starts at once on the Default Media Receiver, and Tellybox keeps using the Default Media Receiver for the next 30 minutes before trying again. The dashboard's **TV receiver** line then reads "Default Media Receiver (Tellybox receiver unavailable until 16:30: reason)". Watching and time limits work the same either way. Common causes:

- The application ID has a typo.
- The Chromecast can't reach GitHub Pages (no internet, or a DNS filter blocks `github.io`), or your own receiver's URL is wrong or not HTTPS.
- The Chromecast can't reach the media URLs: check `TELLYBOX_MEDIA_BASE_URL`.
- With your own receiver app: its settings were changed in the console, and the Chromecast hasn't been rebooted since. The reason then reads `launch failed: CANCELLED`.

Once you've fixed the cause, the fallback ends by itself after 30 minutes, or restart the container (`docker compose restart tellybox`) to retry right away.

### Your own receiver app

Only needed if you change the receiver in a fork, because the shared app always loads this repository's page:

1. **Host the receiver page over HTTPS.** Either:
   - **GitHub Pages of your fork:** in your fork, go to Settings → Pages and set the source to **GitHub Actions**. The `Publish receiver to GitHub Pages` workflow then publishes `tellybox/web/receiver/` to `https://<your-user>.github.io/<your-repo>/receiver/` on every change to it. Run the workflow once by hand for the first publish.
   - **Your own HTTPS host:** the `web` service serves the same files at `/receiver/`, so a reverse proxy address such as `https://tellybox.example.org/receiver/` works (see [HTTPS](#https-with-a-reverse-proxy)). The Chromecast must be able to reach it.
2. **Register as a Cast developer** at the [Google Cast SDK Developer Console](https://cast.google.com/publish). There is a one-time registration fee.
3. **Add a new application**, type **Custom Receiver**, with the receiver URL from step 1 (ending in `/receiver/`). Note the 8-character **Application ID** it shows.
4. **Make it run on your Chromecast.** Either publish the app (this needs a 512×512 icon, such as `tellybox/web/static/icon-512.png`, a title, a description and an HTTPS URL), or add your Chromecast as a test device under *Cast Receiver Devices* using its serial number (in the Google Home app under the device's settings, or on the back of the device). An unpublished app only runs on registered devices. Wait a few minutes, then **reboot the Chromecast** (unplug it for a few seconds).

   **Changed the application's settings later, such as the receiver URL? Reboot the Chromecast again.** It keeps the settings it had until it restarts, and until then every launch fails with `CANCELLED`.
5. **Enter your application ID** under **Settings → Tellybox receiver app ID**, as above.

## HTTPS with a reverse proxy

Plain HTTP on your LAN works fine. To serve Tellybox under a name with HTTPS, put nginx (or another proxy) **on the same host** in front of the web port. Some requirements:

- **Proxy on 127.0.0.1:** Tellybox trusts `X-Forwarded-For` and `X-Forwarded-Proto` only from `127.0.0.1`. The admin sign-in throttle and secure cookies depend on those headers, so the proxy must connect from localhost.
- **Server-Sent Events:** live updates use long-lived streams. Turn off response buffering and allow long reads. A playlist preview can also take a minute.
- **HTTP/2 toward browsers:** every open Tellybox tab keeps one live stream open. Over HTTP/1.1 a browser allows only 6 connections per site, so with 6 tabs open the next page load hangs. HTTP/2 puts all streams on one connection.
- **The Chromecast bypasses the proxy:** leave `TELLYBOX_MEDIA_BASE_URL` pointing at `http://<server-ip>:<web port>`.

An nginx example (1.25.1 or newer; on older versions write `listen 443 ssl http2;` and drop `http2 on;`):

```nginx
server {
    listen 443 ssl;
    http2 on;
    server_name tellybox.example.home;

    ssl_certificate     /etc/ssl/tellybox/fullchain.pem;
    ssl_certificate_key /etc/ssl/tellybox/privkey.pem;

    client_max_body_size 6m;            # artwork and thumbnail uploads (max 5 MB)

    # Optional: LAN and Tailscale only
    allow 192.168.1.0/24;
    allow 100.64.0.0/10;
    deny all;

    location / {
        proxy_pass http://127.0.0.1:8080;
        proxy_http_version 1.1;
        proxy_set_header Connection "";
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_buffering off;             # Server-Sent Events
        proxy_read_timeout 1h;
    }
}
```

## Sign in with OIDC (optional)

If you already sign in to other apps with an identity provider (Pocket ID, Authentik, Keycloak, Authelia, …), the admin pages can use it too. The sign-in page then shows **Sign in with OIDC** above the password form.

- **The password stays.** OIDC is an extra way in. If the provider is down, sign in with the password.
- **One group gets in.** Only members of the group in `TELLYBOX_OIDC_ADMIN_GROUP` are admitted; everyone else at the provider is refused. Without a group, Tellybox doesn't start.
- **Set the password first.** [First-run setup](#4-first-run-setup) still comes first. OIDC sign-in works once a password exists.
- **Not on every platform yet.** The CasaOS, TrueNAS SCALE and Umbrel files don't pass the OIDC settings, so OIDC isn't supported there yet. Unraid, the Home Assistant add-on and Docker Compose support it.
- **A name the browser can reach.** The provider sends the browser back to Tellybox, usually under an HTTPS name on your [reverse proxy](#https-with-a-reverse-proxy).

1. **Register Tellybox at the provider** as an OpenID Connect client (a confidential client with a secret, or a public client: Tellybox always uses PKCE). The redirect or callback URL is your Tellybox address followed by `/admin/oidc/callback`, for example `https://tellybox.example.org/admin/oidc/callback`. Note the client id and secret.
2. **Make the admin group,** for example `tellybox-admins`, and add the parents to it. The provider must send the user's groups, in the ID token or the userinfo response. Most use a `groups` claim with a `groups` scope; for others, see below.
3. **Add the settings** to `.env`:

   ```sh
   TELLYBOX_OIDC_ISSUER=https://id.example.org
   TELLYBOX_OIDC_CLIENT_ID=tellybox
   TELLYBOX_OIDC_CLIENT_SECRET=the-client-secret
   TELLYBOX_OIDC_REDIRECT_URI=https://tellybox.example.org/admin/oidc/callback
   TELLYBOX_OIDC_ADMIN_GROUP=tellybox-admins
   ```

4. **Recreate the container:** `docker compose up -d --force-recreate`. Then open the admin's sign-in page and choose **Sign in with OIDC**.

Some notes per provider:

- **Pocket ID:** add an OIDC client with the callback URL above, and a user group for the parents. The `groups` scope puts the group names in the `groups` claim, so the defaults work.
- **Other providers:** use the group's name exactly as it appears in the claim. Keycloak's group mapper, for example, sends the full path (`/tellybox-admins`) unless you turn that off. If the claim has another name, such as `roles`, set `TELLYBOX_OIDC_GROUPS_CLAIM`. If the provider needs another scope for it, set `TELLYBOX_OIDC_SCOPES`.

Signing out of Tellybox ends only the Tellybox session, not the one at the provider. Sessions last 30 days, as with the password, and a changed `TELLYBOX_ADMIN_PASSWORD` signs out OIDC sessions too.

## Remote access with Tailscale

To use the dashboard's overrides (+15 minutes, block today, stop now) away from home, install [Tailscale](https://tailscale.com) on the server and on your phone. Then open `http://<server's tailscale name>:8080/admin`, or your proxy's name if it allows the Tailscale range (`100.64.0.0/10`). If the server firewall restricts the web port, allow the Tailscale range there too.

Nothing needs to be exposed to the internet.

## Backups and restoring

All state is in one SQLite file. `tellybox backup` makes a consistent, integrity-checked copy while everything keeps running, and keeps the newest N. Schedule it with cron on the host, for example nightly at 02:30:

```cron
30 2 * * *  cd /opt/tellybox && docker compose exec -T tellybox tellybox backup /backups --keep 14
```

Media files aren't in the backup; episodes can be downloaded again. Copy `backups/` somewhere off the machine if you care about history and settings.

To restore:

```sh
cd /opt/tellybox
docker compose stop
cp data/tellybox.db data/tellybox.db.before-restore
rm -f data/tellybox.db-wal data/tellybox.db-shm
cp backups/tellybox-YYYYMMDD-HHMM.db data/tellybox.db
chown 1500:1500 data/tellybox.db   # your PUID:PGID; only the top-level folder is fixed automatically
docker compose start
```

Episodes added after the backup have files in `media/` but no rows in the database. Add them again from the admin pages.

## Updating

```sh
cd /opt/tellybox
docker compose pull
docker compose up -d
```

- **Versions:** the compose file runs `latest`, the newest release. To stay on one version, set `TELLYBOX_TAG=0.1` in `.env` (a minor version gets patch fixes, `0.1.0` never changes). `edge` follows the main branch and may change between releases.
- **Migrations:** database migrations run automatically on start. They only move forward, so take a backup before upgrading if you might want to roll back.
- **yt-dlp** doesn't need an image rebuild. The worker updates it every night at 03:00, and the **Jobs** page has an "Update yt-dlp" button for when YouTube breaks downloads during the day.
- **Housekeeping** also runs at 03:00: viewing history, timer days and the override log are kept for 21 days, and finished jobs for 30.

### Following releases

Each release is announced on the [releases page](https://github.com/sandermvanvliet/Tellybox/releases). On GitHub, **Watch**, then **Custom**, then **Releases** emails you when a new one comes out.

To get a pull request for each new version, pin the image in your compose file (for example `ghcr.io/sandermvanvliet/tellybox:0.1.0`) and let Dependabot watch it. Dependabot's `docker` ecosystem reads compose files, so add this to `.github/dependabot.yml` in the repository that holds your compose file:

```yaml
version: 2
updates:
  - package-ecosystem: "docker"
    directory: "/"
    schedule:
      interval: "weekly"
```

Renovate does the same with its default settings. How releases are made is in [RELEASING.md](RELEASING.md).

### Automatic deploys

The repository contains a GitHub Actions workflow (`.github/workflows/docker-publish.yml`) that tests every push to `main`, builds the image to GHCR and deploys it over SSH (`docker compose pull && docker compose up -d`). To use it for your own fork:
1. Change `IMAGE_NAME` in the workflow, and point `image:` in your compose file at `ghcr.io/<you>/tellybox:edge` (main builds `edge`; `v*` tags build releases and `latest`, and aren't deployed).
2. Create a deploy user on the server with an SSH key.
3. Set the `DEPLOY_HOST`, `DEPLOY_PORT` (the SSH port, usually 22), `DEPLOY_USER`, `DEPLOY_PATH` and `DEPLOY_SSH_KEY` secrets.

## Advanced: separate services

By default, one container runs the web app, the cast service and the worker, and restarts as a whole if one of them stops. If you'd rather run them as three containers, for example to restart one service on its own or to keep separate logs, use this compose file instead. It needs the same `.env` as above (Compose reads `PUID`, `PGID`, `TZ` and the ports from it).

```yaml
x-tellybox: &tellybox
  image: ghcr.io/sandermvanvliet/tellybox:latest
  network_mode: host          # required: mDNS discovery, and the Chromecast fetches media from the host
  restart: unless-stopped
  env_file: .env
  logging:
    driver: json-file
    options: { max-size: "10m", max-file: "3" }

services:
  web:
    <<: *tellybox
    container_name: tellybox-web
    command: ["python", "-m", "tellybox.web"]
    volumes:
      - ./data:/data
      - ./media:/media

  cast:
    <<: *tellybox
    container_name: tellybox-cast
    command: ["python", "-m", "tellybox.cast"]
    volumes:
      - ./data:/data
      - ./media:/media

  worker:
    <<: *tellybox
    container_name: tellybox-worker
    command: ["python", "-m", "tellybox.worker"]
    volumes:
      - ./data:/data
      - ./media:/media
      - ./backups:/backups
```

Some notes:
- **All three services need the same environment.** The web app talks to the cast service on `127.0.0.1:<TELLYBOX_CAST_API_PORT>`, and all three share the database and media folder.
- **The commands in this guide change:** use `docker compose logs web`, `docker compose logs cast` and so on, and run `docker compose exec worker tellybox …` (the `backups/` folder is only mounted in `worker`) and `docker compose exec web tellybox reset-password`.
- **Never start both setups on the same folders.** Run `docker compose down` on one before you switch.

## Troubleshooting

| Symptom | What to check |
| --- | --- |
| **No Chromecast found** | The containers use `network_mode: host`, and the server and Chromecast are on the same subnet (not a guest network with client isolation). Multicast/mDNS isn't blocked between them. `docker compose logs tellybox` shows discovery (look for lines from `tellybox.cast`). |
| **The TV shows the Cast icon but no video, or times out** | The Chromecast can't reach `TELLYBOX_MEDIA_BASE_URL`. Set it to `http://<server LAN IP>:<web port>` and allow that port from the LAN in the firewall. |
| **Sign in with OIDC fails** | If the provider shows an error instead of sending you back, the redirect URI differs from the one registered there (scheme, name or port). Otherwise the page only says it failed, and the reason is in the log: `docker compose logs tellybox \| grep OIDC`. Common causes: the issuer URL is not exactly the provider's, or the server's clock is off. "Your account may not use the Tellybox admin" means the user isn't in `TELLYBOX_OIDC_ADMIN_GROUP`, or the provider doesn't send the groups (see [Sign in with OIDC](#sign-in-with-oidc-optional)). |
| **Forgot the admin password** | If you chose it in the browser: run `docker compose exec tellybox tellybox reset-password`. It clears the password, signs everyone out and prints a new setup code; open `/admin/setup` and choose a new password. (Restarting `web` instead logs a new code, which also works.) If the password comes from `TELLYBOX_ADMIN_PASSWORD` or `_FILE`, change it there and recreate the container (`docker compose up -d --force-recreate`). |
| **Permission denied in the logs** | The container fixes the ownership of `data`, `media` and `backups` at start, so this usually means the folder is read-only or on a filesystem that refuses `chown` (some NFS exports). Set `PUID`/`PGID` to the folder's owner instead. If you run with `user:` in compose, the container can't chown anything and the folders must already belong to that user. |
| **Downloads fail with a YouTube error** | Press "Update yt-dlp" on the **Jobs** page, then retry the job on the **Jobs** page. Private, members-only and age-restricted videos can't be downloaded. |
| **A page hangs while loading, with many Tellybox tabs open** | Your proxy serves HTTP/1.1, and every tab's live stream holds one of the browser's 6 connections. Enable HTTP/2 on the proxy (see [HTTPS](#https-with-a-reverse-proxy)) or close some tabs. |
| **A video still has a sponsor segment** | SponsorBlock's data comes from its users and often arrives after a video is published. Tellybox checks each new video again every night for 7 days and replaces the file when segments are added. The episode's page (**Library** → the show → the episode) shows what was cut, or that SponsorBlock was unreachable or off for it. Videos added before SponsorBlock was available are only cut after **Download again with SponsorBlock** on that page. |
| **Time isn't counted for something playing** | Only playback started from Tellybox is timed. Casting from the YouTube app on a phone is deliberately ignored. |
| **The pages are in the wrong language** | Tellybox follows each browser's language preference (English, Dutch or German; anything else gets English). Change the order of preferred languages in the browser or phone settings. There's no language setting in Tellybox itself. |
| **Port already in use** | Another service owns 8080 or 8081. Change `TELLYBOX_WEB_PORT` and `TELLYBOX_CAST_API_PORT` in `.env`, and `TELLYBOX_MEDIA_BASE_URL` if you set it. |
| **The container keeps restarting** | If one of its three services stops, the container stops and Docker restarts it. `docker compose logs tellybox` names the one that exited (`supervisor: ... exited on its own`). A port already in use is the usual cause. |

## Command-line tools

Everything the admin pages do for content is also available on the command line, inside the container:

```sh
docker compose exec tellybox tellybox add "https://www.youtube.com/watch?v=…"   # preview and queue (add --hold to keep it hidden)
docker compose exec tellybox tellybox jobs                                       # list jobs
docker compose exec tellybox tellybox retry <job id>                             # retry a failed job
docker compose exec tellybox tellybox ytdlp-update                               # queue a yt-dlp update
docker compose exec tellybox tellybox backup /backups --keep 14                  # consistent database backup
docker compose exec tellybox tellybox reset-password                             # forget a browser-chosen admin password, print a setup code
docker compose exec tellybox tellybox migrate                                    # apply migrations (also done on start)
```
