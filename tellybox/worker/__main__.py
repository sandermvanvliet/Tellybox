"""Entry point of the worker service: `python -m tellybox.worker`."""

from __future__ import annotations

import logging
import signal
from collections.abc import Callable

import httpx

from tellybox.clock import SystemClock
from tellybox.config import Config
from tellybox.db import open_db
from tellybox.ingest import JobRunner
from tellybox.worker import Worker
from tellybox.ytdlp import YtDlp


def cast_now_playing(host: str, port: int) -> Callable[[], int | None]:
    """The episode loaded on the TV according to the cast service, for deferring a file swap (SB-3).

    Raises when the cast service can't be reached; the job then waits.
    """
    url = f"http://{host}:{port}/state"

    def now_playing() -> int | None:
        response = httpx.get(url, timeout=5)
        response.raise_for_status()
        playing = response.json().get("now_playing")
        return playing.get("episode_id") if playing else None

    return now_playing


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    config = Config.from_env()
    conn = open_db(config.db_path)
    tools_dir = config.data_dir / "tools" / "yt-dlp"
    runner = JobRunner(conn=conn, media_dir=config.media_dir, tools_dir=tools_dir, ytdlp=YtDlp(tools_dir), clock=SystemClock(),
                       now_playing=cast_now_playing(config.cast_api_host, config.cast_api_port))
    worker = Worker(runner, config.tz)
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: worker.stop.set())
    worker.run_forever()


if __name__ == "__main__":
    main()
