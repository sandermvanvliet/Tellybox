"""Entry point of the cast service: `python -m tellybox.cast`."""

from __future__ import annotations

import asyncio
import logging
import signal

import uvicorn

from tellybox import store
from tellybox.cast.api import create_api
from tellybox.cast.controller import CastController
from tellybox.cast.device import DeviceInfo
from tellybox.cast.pychromecast_device import PyChromecastDevice, discover
from tellybox.clock import SystemClock
from tellybox.config import Config
from tellybox.db import open_db

log = logging.getLogger("tellybox.cast")

DISCOVERY_RETRY_S = 60.0


async def choose_device(conn, clock) -> DeviceInfo | None:
    """The remembered device, or the only one on the network (PB-1)."""
    if (info := store.selected_device(conn)) is not None:
        return info
    found = await discover()
    store.remember_devices(conn, found, clock.now())
    if len(found) == 1:
        store.select_device(conn, found[0].uuid)
        log.info("selected the only Chromecast found: %s (%s)", found[0].name, found[0].host)
        return found[0]
    if found:
        log.warning("%d Chromecasts found; choose one via POST /devices/select", len(found))
    else:
        log.warning("no Chromecast found; retrying every %.0f s", DISCOVERY_RETRY_S)
    return None


async def main() -> None:
    config = Config.from_env()
    conn = open_db(config.db_path)
    clock = SystemClock()
    controller = CastController(
        conn, clock=clock, tz=config.tz, media_base_url=config.media_base_url, secret=config.secret,
        device_factory=PyChromecastDevice,
    )
    await controller.start()

    async def attach_device() -> None:
        while controller.device is None:
            info = await choose_device(conn, clock)
            if info is not None and controller.device is None:
                await controller.set_device(PyChromecastDevice(info))
                return
            await asyncio.sleep(DISCOVERY_RETRY_S)

    attach_task = asyncio.create_task(attach_device())
    api = create_api(controller, conn, discover=discover, device_factory=PyChromecastDevice)
    server = uvicorn.Server(uvicorn.Config(
        api, host=config.cast_api_host, port=config.cast_api_port, log_config=None, access_log=False,
        timeout_graceful_shutdown=2,  # the web service keeps an SSE stream open; don't block restarts
    ))
    server.install_signal_handlers = lambda: None
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    serve_task = asyncio.create_task(server.serve())
    log.info("cast API on http://%s:%d; media base %s", config.cast_api_host, config.cast_api_port, config.media_base_url)
    await stop.wait()
    server.should_exit = True
    attach_task.cancel()
    await controller.stop_service()
    await serve_task


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    logging.getLogger("pychromecast").setLevel(logging.WARNING)
    asyncio.run(main())
