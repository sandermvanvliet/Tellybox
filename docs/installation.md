# <picture><source media="(prefers-color-scheme: dark)" srcset="images/brand/mark-dark.svg"><img src="images/brand/mark.svg" alt="" width="36" align="top"></picture> Installing and deploying Tellybox

This guide takes you from an empty server to kids picking videos on the TV. Setup takes 15 to 30 minutes. Then it covers HTTPS, remote access, backups, updates and troubleshooting.

- [What you need](#what-you-need)
- [1. Get the image](#1-get-the-image)
- [2. Prepare the folders](#2-prepare-the-folders)
- [3. Write the compose file](#3-write-the-compose-file)
- [4. Start it](#4-start-it)
- [5. First-run setup](#5-first-run-setup)
- [Configuration reference](#configuration-reference)
- [HTTPS with a reverse proxy](#https-with-a-reverse-proxy)
- [Remote access with Tailscale](#remote-access-with-tailscale)
- [Backups and restoring](#backups-and-restoring)
- [Updating](#updating)
- [Troubleshooting](#troubleshooting)
- [Command-line tools](#command-line-tools)

## What you need

| | |
| --- | --- |
| **Server** | A Linux machine that stays on, with Docker Engine and the Compose plugin (a home server, NAS or mini PC). Tellybox is developed on Ubuntu and Fedora. No GPU is needed. |
| **Chromecast** | Any Chromecast or Google TV that plays through the standard Cast "Default Media Receiver". Even the original 2013 Chromecast works. |
| **Network** | The server and the Chromecast on the **same LAN segment**. Tellybox finds the Chromecast with mDNS, and the Chromecast downloads the video straight from the server. |
| **Disk** | About 0.5 to 1 GB per hour of video (720p H.264). |
| **Ports** | One TCP port for the web app (8080 by default), reachable from your LAN, including the Chromecast. A second port (8081 by default) is used on localhost only. |

> **Docker Desktop on macOS or Windows doesn't work.** Tellybox needs `network_mode: host` for mDNS discovery, and Docker Desktop runs containers in a VM, where host networking can't reach your LAN. Use a Linux host.

## 1. Get the image

Tellybox is one image that runs three services (`web`, `cast` and `worker`). Build it from source:

```sh
git clone https://github.com/sandermvanvliet/Tellybox.git
cd Tellybox
docker build -t tellybox:latest --build-arg APP_VERSION=$(git describe --always) .
```

`APP_VERSION` is optional. It's shown on the admin dashboard and at `/healthz`.

## 2. Prepare the folders

Pick a home for Tellybox; this guide uses `/opt/tellybox`. The containers run as **uid/gid 1500**, so that user must own the data folders:

```sh
sudo mkdir -p /opt/tellybox/{data,media,backups,secrets}
sudo chown 1500:1500 /opt/tellybox/{data,media,backups}
```

| Folder | Contents |
| --- | --- |
| `data/` | The SQLite database, the media-link signing key and the self-updating yt-dlp install |
| `media/` | Episodes, thumbnails and show artwork |
| `backups/` | Database backups (see [Backups](#backups-and-restoring)) |
| `secrets/` | The admin password file |

Store the admin password as a file, so it doesn't end up in the compose file or your shell history:

```sh
sudo sh -c 'read -r -s -p "Admin password: " p && printf %s "$p" > /opt/tellybox/secrets/admin-password'
sudo chown 1500:1500 /opt/tellybox/secrets/admin-password
sudo chmod 400 /opt/tellybox/secrets/admin-password
```

## 3. Write the compose file

Save this as `/opt/tellybox/docker-compose.yml`. Replace `192.168.1.10` with the server's LAN address and set your time zone.

```yaml
x-tellybox: &tellybox
  image: tellybox:latest
  network_mode: host          # required: mDNS discovery, and the Chromecast fetches media from the host
  restart: unless-stopped
  logging:
    driver: json-file
    options: { max-size: "10m", max-file: "3" }

x-env: &env
  TELLYBOX_WEB_PORT: "8080"
  TELLYBOX_CAST_API_PORT: "8081"
  TELLYBOX_MEDIA_BASE_URL: "http://192.168.1.10:8080"   # how the Chromecast reaches the server
  TELLYBOX_DATA_DIR: /data
  TELLYBOX_MEDIA_DIR: /media
  TZ: Europe/Amsterdam                                   # the daily reset and history use local time

services:
  web:
    <<: *tellybox
    container_name: tellybox-web
    command: ["python", "-m", "tellybox.web"]
    volumes:
      - ./data:/data
      - ./media:/media
      - ./secrets/admin-password:/run/secrets/admin-password:ro
    environment:
      <<: *env
      TELLYBOX_ADMIN_PASSWORD_FILE: /run/secrets/admin-password
    healthcheck:
      test: ["CMD", "python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=3)"]
      interval: 30s
      timeout: 5s
      retries: 3
      start_period: 10s

  cast:
    <<: *tellybox
    container_name: tellybox-cast
    command: ["python", "-m", "tellybox.cast"]
    volumes:
      - ./data:/data
      - ./media:/media
    environment: *env

  worker:
    <<: *tellybox
    container_name: tellybox-worker
    command: ["python", "-m", "tellybox.worker"]
    volumes:
      - ./data:/data
      - ./media:/media
      - ./backups:/backups
    environment: *env
```

Some notes on this file:
- **All three services need the same environment.** The web app talks to the cast service on `127.0.0.1:<TELLYBOX_CAST_API_PORT>`, and all three share the database and media folder.
- **Pick free ports.** 8080 and 8081 are popular (UniFi uses both, for example). Change `TELLYBOX_WEB_PORT`, `TELLYBOX_MEDIA_BASE_URL` and the healthcheck together.
- **On Fedora or RHEL with SELinux**, add `:z` to each bind mount (`./data:/data:z`).
- **Run only one Tellybox per Chromecast.** Two cast services will fight over the same TV.

## 4. Start it

```sh
cd /opt/tellybox
docker compose up -d
docker compose ps                      # web should become "healthy" within a minute
docker compose logs -f cast            # watch it find your Chromecast
```

The database is created and migrated on first start. The worker installs its own updatable copy of yt-dlp into `data/tools/`, which takes a few seconds.

### Firewall

If the server runs a firewall, allow the web port from your LAN only. The Chromecast needs to reach it. With `ufw`:

```sh
sudo ufw allow from 192.168.1.0/24 to any port 8080 proto tcp
```

The cast API (8081) listens on `127.0.0.1` only, so it needs no rule. mDNS discovery works through outgoing multicast; with the default ufw policy (outgoing allowed) nothing else is needed.

**Don't forward any port from your router.** Tellybox has no internet-facing features, and the kid app has no login by design. Anyone who can reach it can play approved videos.

## 5. First-run setup

1. **Sign in:** open `http://<server>:8080/admin` and sign in with the admin password.
2. **Choose the TV:** in **Settings**, pick your Chromecast. With only one on the network, it's chosen automatically. Tellybox remembers it and reconnects on its own after restarts or power cuts.
3. **Set the limits:** also in **Settings**:
   - the daily allowance (default 60 minutes);
   - how time is counted (only playing time, or wall clock);
   - the longest viewing session (default 90 minutes);
   - the finishing grace (default 15 minutes);
   - the reset time (default 04:00).
4. **Add something:** in **Add**, paste a YouTube video or playlist link. Check the preview and add it. Downloads show up under **Jobs**. Anything added with "hold" appears under **Library → Held downloads** until you publish it.
5. **Give the kids the app:** open `http://<server>:8080` on their tablet or phone and use "Add to Home Screen". It opens full screen like an app.
6. **Try it:** tap an episode. The TV should start within a few seconds.

## Configuration reference

All settings are environment variables. What you can change in the admin pages (allowances, timer rules, the Chromecast) lives in the database instead.

| Variable | Default | Meaning |
| --- | --- | --- |
| `TELLYBOX_ADMIN_PASSWORD_FILE` | none | A file containing the admin password (preferred). |
| `TELLYBOX_ADMIN_PASSWORD` | none | The admin password itself. If neither is set, the admin pages stay locked. Changing the password signs out every session. |
| `TELLYBOX_WEB_PORT` | `8080` | Port for the kid app, the admin pages and the media files. |
| `TELLYBOX_WEB_HOST` | `0.0.0.0` | Address the web app listens on. |
| `TELLYBOX_MEDIA_BASE_URL` | `http://<LAN IP>:<web port>` | The URL the Chromecast uses to fetch videos. It's detected automatically. Set it explicitly if the server has several interfaces (Docker bridges, VPNs) or the detection picks the wrong one. Use plain `http://` and an IP address: a Chromecast can't resolve local-only DNS names. |
| `TELLYBOX_CAST_API_PORT` | `8081` | Internal API of the cast service. |
| `TELLYBOX_CAST_API_HOST` | `127.0.0.1` | Keep this on localhost. |
| `TELLYBOX_DATA_DIR` | `/data` (in the image) | Database, signing key and yt-dlp install. |
| `TELLYBOX_MEDIA_DIR` | `/media` (in the image) | Video files and images. |
| `TELLYBOX_DB` | `<data>/tellybox.db` | Database path. |
| `TELLYBOX_SECRET_FILE` | `<data>/secret.key` | Key for signing media links. It's created on first start. Media links stay valid across restarts for 24 hours. |
| `TZ` or `TELLYBOX_TZ` | the host's zone, else UTC | Local time zone for the daily reset, schedules and history. |

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

## Remote access with Tailscale

To use the dashboard's overrides (+15 minutes, block today, stop now) away from home, install [Tailscale](https://tailscale.com) on the server and on your phone. Then open `http://<server's tailscale name>:8080/admin`, or your proxy's name if it allows the Tailscale range (`100.64.0.0/10`). If the server firewall restricts the web port, allow the Tailscale range there too.

Nothing needs to be exposed to the internet.

## Backups and restoring

All state is in one SQLite file. `tellybox backup` makes a consistent, integrity-checked copy while everything keeps running, and keeps the newest N. Schedule it with cron on the host, for example nightly at 02:30:

```cron
30 2 * * *  cd /opt/tellybox && docker compose exec -T worker tellybox backup /backups --keep 14
```

Media files aren't in the backup; episodes can be downloaded again. Copy `backups/` somewhere off the machine if you care about history and settings.

To restore:

```sh
cd /opt/tellybox
docker compose stop
cp data/tellybox.db data/tellybox.db.before-restore
rm -f data/tellybox.db-wal data/tellybox.db-shm
cp backups/tellybox-YYYYMMDD-HHMM.db data/tellybox.db
chown 1500:1500 data/tellybox.db
docker compose start
```

Episodes added after the backup have files in `media/` but no rows in the database. Add them again from the admin pages.

## Updating

```sh
cd ~/Tellybox && git pull
docker build -t tellybox:latest --build-arg APP_VERSION=$(git describe --always) .
cd /opt/tellybox && docker compose up -d
```

- **Migrations:** database migrations run automatically on start. They only move forward, so take a backup before upgrading if you might want to roll back.
- **yt-dlp** doesn't need an image rebuild. The worker updates it every night at 03:00, and the **Jobs** page has an "Update yt-dlp" button for when YouTube breaks downloads during the day.
- **Housekeeping** also runs at 03:00: viewing history, timer days and the override log are kept for 21 days, and finished jobs for 30.

### Automatic deploys

The repository contains a GitHub Actions workflow (`.github/workflows/docker-publish.yml`) that tests every push to `main`, builds the image to GHCR and deploys it over SSH (`docker compose pull && docker compose up -d`). To use it for your own fork:
1. Change `IMAGE_NAME` in the workflow, and point `image:` in your compose file at `ghcr.io/<you>/tellybox:latest`.
2. Create a deploy user on the server with an SSH key.
3. Set the `DEPLOY_HOST`, `DEPLOY_PORT` (the SSH port, usually 22), `DEPLOY_USER`, `DEPLOY_PATH` and `DEPLOY_SSH_KEY` secrets.

## Troubleshooting

| Symptom | What to check |
| --- | --- |
| **No Chromecast found** | The containers use `network_mode: host`, and the server and Chromecast are on the same subnet (not a guest network with client isolation). Multicast/mDNS isn't blocked between them. `docker compose logs cast` shows discovery. |
| **The TV shows the Cast icon but no video, or times out** | The Chromecast can't reach `TELLYBOX_MEDIA_BASE_URL`. Set it to `http://<server LAN IP>:<web port>` and allow that port from the LAN in the firewall. |
| **Admin says "Admin is locked"** | No password is configured. Set `TELLYBOX_ADMIN_PASSWORD_FILE` (readable by uid 1500) or `TELLYBOX_ADMIN_PASSWORD` on the `web` service and recreate it. |
| **Permission denied in the logs** | The data folders must be owned by uid/gid 1500: `sudo chown -R 1500:1500 data media backups`. |
| **Downloads fail with a YouTube error** | Press "Update yt-dlp" on the **Jobs** page, then retry the job on the **Jobs** page. Private, members-only and age-restricted videos can't be downloaded. |
| **A page hangs while loading, with many Tellybox tabs open** | Your proxy serves HTTP/1.1, and every tab's live stream holds one of the browser's 6 connections. Enable HTTP/2 on the proxy (see [HTTPS](#https-with-a-reverse-proxy)) or close some tabs. |
| **Time isn't counted for something playing** | Only playback started from Tellybox is timed. Casting from the YouTube app on a phone is deliberately ignored. |
| **The pages are in the wrong language** | Tellybox follows each browser's language preference (English, Dutch or German; anything else gets English). Change the order of preferred languages in the browser or phone settings. There's no language setting in Tellybox itself. |
| **Port already in use** | Another service owns 8080 or 8081. Change `TELLYBOX_WEB_PORT` and `TELLYBOX_CAST_API_PORT` (and the media URL and healthcheck). |

## Command-line tools

Everything the admin pages do for content is also available on the command line, inside any of the containers:

```sh
docker compose exec worker tellybox add "https://www.youtube.com/watch?v=…"   # preview and queue (add --hold to keep it hidden)
docker compose exec worker tellybox jobs                                       # list jobs
docker compose exec worker tellybox retry <job id>                             # retry a failed job
docker compose exec worker tellybox ytdlp-update                               # queue a yt-dlp update
docker compose exec worker tellybox backup /backups --keep 14                  # consistent database backup
docker compose exec worker tellybox migrate                                    # apply migrations (also done on start)
```
