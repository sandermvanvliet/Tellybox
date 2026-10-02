"""Container healthcheck: `python -m tellybox.healthcheck` (exit 0 healthy, 1 not).

Looks at what PID 1 runs: the web service (or the future all-in-one `-m tellybox`) must
answer /healthz, the cast service /state; the worker has no endpoint and counts as healthy.
"""

from __future__ import annotations

import os
import sys
import urllib.request
from collections.abc import Callable, Mapping


def read_cmdline(path: str = "/proc/1/cmdline") -> list[str]:
    try:
        with open(path, "rb") as f:
            return [a.decode(errors="replace") for a in f.read().split(b"\0") if a]
    except OSError:
        return []


def target_url(cmdline: list[str], env: Mapping[str, str]) -> str | None:
    """The URL to probe, or None when there's nothing to check."""
    if "-m" not in cmdline:
        return None
    i = cmdline.index("-m")
    module = cmdline[i + 1] if i + 1 < len(cmdline) else ""
    if module in ("tellybox.web", "tellybox"):
        return f"http://127.0.0.1:{env.get('TELLYBOX_WEB_PORT', '8080')}/healthz"
    if module == "tellybox.cast":
        host = env.get("TELLYBOX_CAST_API_HOST", "127.0.0.1")
        return f"http://{host}:{env.get('TELLYBOX_CAST_API_PORT', '8081')}/state"
    return None


def check(
    cmdline: list[str] | None = None,
    env: Mapping[str, str] | None = None,
    urlopen: Callable = urllib.request.urlopen,
) -> bool:
    cmdline = read_cmdline() if cmdline is None else cmdline
    env = os.environ if env is None else env
    url = target_url(cmdline, env)
    if url is None:
        return True
    try:
        with urlopen(url, timeout=3) as resp:
            return 200 <= resp.status < 300
    except Exception:
        return False


def main() -> None:
    sys.exit(0 if check() else 1)


if __name__ == "__main__":
    main()
