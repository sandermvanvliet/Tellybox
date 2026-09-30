# Receiver spike: findings (step 13)

_2026-09-30 · run from the Fedora dev machine on the same LAN as the Chromecast, with a dev web service (`python -m tellybox.web`) serving a test clip over HTTP._

**Device:** "Living Room TV", original 1st-gen Chromecast, cast firmware 1.56.
**Receiver:** the spike page `https://sandermvanvliet.github.io/Tellybox/receiver/spike.html` (GitHub Pages), registered as an unpublished Custom Receiver with the device as a test device.
**Media:** `tellybox dev make-clips`, 2-minute 1280×720 H.264 Main@4.0 + AAC, faststart, over plain HTTP from the LAN.
**Tool:** `scripts/receiver_spike.py` (pychromecast).

**Verdict:** our own receiver works on the 1st-gen Chromecast, hosted on GitHub Pages, with media and images over plain HTTP from the LAN. CAF runs (the legacy-SDK fallback isn't needed), the custom namespace works both ways, the media controller loads into our app, and the corner overlay costs no frames. No change of hosting is needed.

## Checklist results

| # | Check | Result | Notes |
|---|---|---|---|
| S1 | Pages page launches on the 1st gen | ✅ | `start_app` returned after **3.2 s** (from the Default Media Receiver). The Default Media Receiver itself takes 2.5 s. |
| S2 | CAF runs | ✅ | CAF **3.0.0156**. UA: `Mozilla/5.0 (X11; Linux armv7l) … Chrome/90.0.4430.225 Safari/537.36 CrKey/1.56.500000`. `setMediaElement` on our own `<video>` works. `spike-v2.html` isn't needed. |
| S3 | HTTP media and images from the HTTPS page | ✅ | Both load: the clip over HTTP with Range requests (206), and a JPEG over HTTP ("image ok 480x270"). No mixed-content blocking. |
| S4 | Namespace round trip | ✅ | `urn:x-cast:tellybox` is listed in the app's namespaces. The page sends `hello` on sender connect, and ping/pong round-trips in ~20 ms. |
| S5 | `play_media` loads into our app (WT-9) | ✅ | Status shows `app_id=55AAC641` (ours), a session id, our signed `content_id`, `PLAYING`. |
| S6 | 720p for 2 minutes, overlay on vs off | ✅ | **0 dropped of 2736** frames with the corner sky and moving sun; 0 of 2737 without. No stalls. |
| S7 | Idle 10+ minutes with `disableIdleTimeout`; `quit_app` | ✅ | The page stayed up for **11 minutes** with no media (after another ~4.5 minutes idle from the S8 run), still reporting stats every 10 s. After `quit_app`, `app_id` is `None` at once, and the device then returns to Backdrop (`E8C28D3C`). |
| S8 | Wrong or unregistered app id | ✅ | `start_app("00000000")` raises `RequestFailed('Failed to execute start app 00000000.')` after **0.02 s**. The running app stays. |
| S9 | Tap-to-playing, cold and warm (NF-5) | ✅ | Default Media Receiver: 2.5 s launch + 1.45 s = **~4.0 s** cold. Ours: 3.2 s + 1.10 s = **~4.3 s** cold, **0.70 s** warm. Both are within the 5 s target. |

## CAF API check

Through remote DevTools on the registered device (`http://<chromecast ip>:9222`), the calls in `receiver/cast.js` that the spike page doesn't use exist in CAF 3.0.0156:
- the event types `BUFFERING`, `MEDIA_FINISHED`, `TIME_UPDATE` and `PAUSE`;
- `PlayerManager.getPlayerState()`, which returns `IDLE`, `PLAYING`, `PAUSED` or `BUFFERING`.

## What this changes

1. **Hosting:** GitHub Pages stays. No public DNS name or HTTPS media is needed on the home server.
2. **`_launch_receiver`:** a failed launch is a `RequestFailed`, which is a `PyChromecastError`, so it already becomes `ReceiverUnavailable`. The failure arrives within milliseconds, so the fallback to the Default Media Receiver is quick. `RECEIVER_LAUNCH_TIMEOUT_S` stays at 8 s: the cold launch takes 3.2 s.
3. **`receiver/cast.js`:** no changes needed for the SDK calls or event names.
4. **No warm-up:** cold tap-to-playing is within NF-5, so we don't launch the receiver when a kid page opens.
5. **Spike script fix:** `get_listed_chromecasts` with neither names nor UUIDs matches nothing, and stopping the mDNS browser before connecting breaks the connection. The script now uses `get_chromecasts(known_hosts=…)` and keeps the browser running. The script is removed with the spike pages.
