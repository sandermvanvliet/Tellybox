# Platform templates

Ready-made app entries for NAS and home-server platforms. All of them run the same image, `ghcr.io/sandermvanvliet/tellybox`, with host networking (Tellybox finds the Chromecast with mDNS). Linux only. See [docs/installation.md](../../docs/installation.md#other-platforms) for the full guide.

- **Unraid** (`unraid/tellybox.xml`): a Community Applications template. Until it is listed in the store, save it as `/boot/config/plugins/dockerMan/templates-user/tellybox.xml` on the Unraid flash drive, then add the container from **Docker > Add Container** and pick the Tellybox template. Check the paths and time zone, then apply.
- **CasaOS** (`casaos/docker-compose.yml`): a compose file with `x-casaos` metadata. In CasaOS, choose **App Store > Custom Install** (the import icon), paste the file and install. Edit the time zone first.
- **TrueNAS SCALE** (`truenas/docker-compose.yml`): for SCALE 24.10 or newer. Create a dataset with `data`, `media` and `backups` folders, change `tank` in the paths to your pool, then use **Apps > Discover Apps > menu > Install via YAML** and paste the file.
- **Umbrel** (`umbrel/tellybox/`): an app folder (`umbrel-app.yml` and `docker-compose.yml`) for a community app store or for the official one. It is not installable from this repository alone: it needs a pinned release and image digest first (see the TODOs in the files).
- **Synology**: no file needed. Use the release `docker-compose.yml` in Container Manager (see the installation guide).
