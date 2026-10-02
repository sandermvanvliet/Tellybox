"""All-in-one container: `python -m tellybox` runs web, cast and worker (DP-5).

A small supervisor. The children inherit stdout/stderr, so their logs reach `docker logs`
unchanged. SIGTERM/SIGINT go to every child; if one exits on its own, the others are
stopped and we exit non-zero so Docker's restart policy restarts the whole container.
(The services migrate the database themselves; concurrent starts are safe, see db.migrate.)
"""

from __future__ import annotations

import logging
import signal
import subprocess
import sys
import time

log = logging.getLogger("tellybox.supervisor")

SERVICES = ("web", "cast", "worker")
STOP_TIMEOUT_S = 10.0


def service_commands() -> dict[str, list[str]]:
    return {name: [sys.executable, "-m", f"tellybox.{name}"] for name in SERVICES}


def _stop_all(procs: dict[str, subprocess.Popen], timeout: float) -> None:
    for p in procs.values():
        if p.poll() is None:
            p.terminate()
    deadline = time.monotonic() + timeout
    for name, p in procs.items():
        try:
            p.wait(max(0.0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            log.warning("supervisor: %s ignored SIGTERM, killing it", name)
            p.kill()
            p.wait()


def supervise(
    commands: dict[str, list[str]],
    stop_timeout: float = STOP_TIMEOUT_S,
    poll_interval: float = 0.2,
) -> int:
    """Run the commands until a signal arrives or one exits. Returns the exit code."""
    stopping: list[int] = []

    def on_signal(signum: int, _frame) -> None:
        stopping.append(signum)

    old = {s: signal.signal(s, on_signal) for s in (signal.SIGTERM, signal.SIGINT)}
    procs: dict[str, subprocess.Popen] = {}
    try:
        for name, cmd in commands.items():
            procs[name] = subprocess.Popen(cmd)
            log.info("supervisor: started %s (pid %s)", name, procs[name].pid)
        code = 0
        while not stopping:
            dead = [(n, p.returncode) for n, p in procs.items() if p.poll() is not None]
            if dead:
                for name, rc in dead:
                    log.error("supervisor: %s exited on its own with code %s", name, rc)
                code = 1
                break
            time.sleep(poll_interval)
        if stopping:
            log.info("supervisor: signal %s, stopping %s", stopping[0], ", ".join(procs))
        _stop_all(procs, stop_timeout)
        return code
    finally:
        for p in procs.values():
            if p.poll() is None:
                p.kill()
        for s, h in old.items():
            signal.signal(s, h)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stderr)
    sys.exit(supervise(service_commands()))


if __name__ == "__main__":
    main()
