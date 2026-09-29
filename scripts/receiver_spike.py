#!/usr/bin/env python3
"""Step 13 spike (docs/plans/step13-receiver.md, S1..S9): try a Cast receiver app on the real Chromecast.

Launches `--app-id` (the Tellybox receiver's spike page, or CC1AD845 for the Default Media Receiver as a
baseline), loads a signed media URL from the local dev web service, talks to the page on the custom
namespace and prints everything the page reports: log lines, user agent, dropped frames.

Run it next to a dev web service (same TELLYBOX_* env, so the media URL is signed with the same secret):

    .venv/bin/python -m tellybox.web &                  # serves /media and /img on the LAN
    .venv/bin/python scripts/receiver_spike.py --host <chromecast-ip> --app-id <APPID> --episode 1

Temporary: removed with the spike pages once the spike is done.
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from datetime import UTC, datetime

import pychromecast
from pychromecast.controllers import BaseController
from pychromecast.error import PyChromecastError

from tellybox.config import Config
from tellybox.media_urls import media_url

NS = "urn:x-cast:tellybox"
DEFAULT_MEDIA_RECEIVER = "CC1AD845"
T0 = time.monotonic()


def say(msg: str) -> None:
    print(f"{time.monotonic() - T0:7.2f}s  {msg}", flush=True)


class SpikeController(BaseController):
    """Prints every message from the receiver page and remembers the first hello."""

    def __init__(self) -> None:
        super().__init__(NS)
        self.hello = threading.Event()
        self.stats: list[dict] = []

    def receive_message(self, _message, data: dict) -> bool:
        kind = data.get("type")
        if kind == "hello":
            self.hello.set()
            say(f"<- hello, ua: {data.get('ua')}")
        elif kind == "stats":
            self.stats.append(data)
            say(f"<- stats dropped={data.get('dropped')} total={data.get('total')} t={data.get('t')}")
        elif kind == "log":
            say(f"<- page: {data.get('msg')}")
        else:
            say(f"<- {json.dumps(data)}")
        return True

    def send(self, data: dict) -> None:
        say(f"-> {json.dumps(data)}")
        try:
            self.send_message(data)
        except PyChromecastError as exc:
            say(f"send failed: {exc!r}")


def find(host: str):
    casts, browser = pychromecast.get_listed_chromecasts(known_hosts=[host], discovery_timeout=8)
    browser.stop_discovery()
    for c in casts:
        if c.cast_info.host == host:
            return c
    sys.exit(f"no Chromecast at {host} (found: {[c.cast_info.host for c in casts]})")


def wait_for(predicate, timeout: float, step: float = 0.05) -> float | None:
    start = time.monotonic()
    while time.monotonic() - start < timeout:
        if predicate():
            return time.monotonic() - start
        time.sleep(step)
    return None


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", required=True, help="the Chromecast's IP address")
    ap.add_argument("--app-id", required=True, help="receiver app id (CC1AD845 = Default Media Receiver)")
    ap.add_argument("--episode", type=int, default=1, help="episode id served by the local web service")
    ap.add_argument("--base", help="media base URL (default: TELLYBOX_MEDIA_BASE_URL or http://<lan ip>:<port>)")
    ap.add_argument("--seconds", type=int, default=120, help="how long to play and collect stats (S6)")
    ap.add_argument("--overlay", choices=("on", "off"), default="on", help="corner sky with a moving sun (S6)")
    ap.add_argument("--no-play", action="store_true", help="only launch and talk to the page (S7: leave it idle)")
    ap.add_argument("--quit", action="store_true", help="quit the app at the end (S7: back to the backdrop)")
    args = ap.parse_args()

    config = Config.from_env()
    base = (args.base or config.media_base_url).rstrip("/")
    url = media_url(base, config.secret, args.episode, datetime.now(UTC))
    image = f"{base}/img/episode/{args.episode}.jpg"

    cast = find(args.host)
    cast.wait(timeout=15)
    say(f"connected to {cast.cast_info.friendly_name} ({cast.cast_info.model_name}); running app {cast.app_id}")
    ctl = SpikeController()
    cast.register_handler(ctl)

    # S1, S8, S9: launch time, or how a failed launch is reported.
    t = time.monotonic()
    try:
        cast.start_app(args.app_id, timeout=20)
    except PyChromecastError as exc:
        say(f"LAUNCH FAILED after {time.monotonic() - t:.2f}s: {exc!r}")
        return
    ready = wait_for(lambda: cast.app_id == args.app_id, 20)
    say(f"launched {args.app_id}: start_app returned after {time.monotonic() - t:.2f}s, app_id seen after {ready}")
    say(f"app namespaces: {cast.socket_client.app_namespaces}")

    # S4: round trip.
    if args.app_id != DEFAULT_MEDIA_RECEIVER:
        if wait_for(ctl.hello.is_set, 10) is None:
            say("no hello within 10 s (page not up, or namespace not declared)")
        ctl.send({"type": "ping", "v": 1, "echo": "round-trip"})
        ctl.send({"type": "image", "v": 1, "url": image})  # S3 (images)
        ctl.send({"type": "overlay", "v": 1, "on": args.overlay == "on"})
        ctl.send({"type": "logview", "v": 1, "on": args.no_play})  # keep the log off the video for S6

    if not args.no_play:
        # S3, S5, S9: our media loads into this app (not the Default Media Receiver) and plays.
        mc = cast.media_controller
        t = time.monotonic()
        mc.play_media(url, "video/mp4", title="spike", stream_type="BUFFERED")
        playing = wait_for(lambda: mc.status.player_state == "PLAYING", 30)
        st = mc.status
        say(f"tap-to-playing {playing if playing is None else f'{playing:.2f}s'}; app_id={cast.app_id} "
            f"session={cast.status.session_id if cast.status else None} content_id={st.content_id} "
            f"state={st.player_state} idle_reason={st.idle_reason}")
        end = time.monotonic() + args.seconds
        while time.monotonic() < end:
            time.sleep(5)
            mc.update_status()
            say(f"   media {mc.status.player_state} t={mc.status.adjusted_current_time}")
        if ctl.stats:
            first, last = ctl.stats[0], ctl.stats[-1]
            say(f"S6 frames over the run: dropped {last['dropped'] - first['dropped']} of "
                f"{last['total'] - first['total']} (overlay {args.overlay})")
    else:
        say(f"leaving the app idle for {args.seconds}s (S7)")
        for _ in range(args.seconds // 30):
            time.sleep(30)
            say(f"   still running: app_id={cast.app_id}")

    if args.quit:
        cast.quit_app()
        say(f"quit; app_id now {cast.app_id} (after {wait_for(lambda: cast.app_id != args.app_id, 10)})")
    cast.disconnect(timeout=5)


if __name__ == "__main__":
    main()
