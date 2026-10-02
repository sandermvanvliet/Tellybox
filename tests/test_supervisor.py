"""The all-in-one supervisor (DP-5), with fake child commands."""

import os
import signal
import sys
import threading
import time

from tellybox.__main__ import SERVICES, service_commands, supervise

SLEEP = [sys.executable, "-c", "import time; time.sleep(30)"]
STUBBORN = [
    sys.executable, "-c",
    "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(30)",
]


def _signal_later(sig, delay=0.5):
    threading.Timer(delay, lambda: os.kill(os.getpid(), sig)).start()


def test_commands_cover_all_services():
    cmds = service_commands()
    assert tuple(cmds) == SERVICES
    assert cmds["cast"] == [sys.executable, "-m", "tellybox.cast"]


def test_sigterm_fans_out_and_exits_cleanly():
    _signal_later(signal.SIGTERM)
    t0 = time.monotonic()
    assert supervise({"a": SLEEP, "b": SLEEP}, stop_timeout=5) == 0
    assert time.monotonic() - t0 < 5


def test_child_dying_stops_the_others_and_fails():
    cmds = {"a": SLEEP, "b": [sys.executable, "-c", "import time; time.sleep(0.5); raise SystemExit(3)"]}
    t0 = time.monotonic()
    assert supervise(cmds, stop_timeout=5) == 1
    assert time.monotonic() - t0 < 5


def test_child_ignoring_sigterm_is_killed_after_timeout():
    _signal_later(signal.SIGTERM, delay=1.0)
    t0 = time.monotonic()
    assert supervise({"a": STUBBORN}, stop_timeout=0.5) == 0
    assert time.monotonic() - t0 < 5
