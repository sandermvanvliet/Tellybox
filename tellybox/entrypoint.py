"""Container entrypoint: `python -m tellybox.entrypoint [CMD...]` (DP-3).

Not root (compose `user:` or `docker run --user`): exec the command unchanged.
Root: chown the data folders if needed, drop to PUID/PGID, exec the command.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path

from tellybox import privileges

log = logging.getLogger("tellybox.entrypoint")

DEFAULT_COMMAND = ["python", "-m", "tellybox.web"]


def data_dirs(env: Mapping[str, str]) -> list[Path]:
    return [
        Path(env.get("TELLYBOX_DATA_DIR", "/data")),
        Path(env.get("TELLYBOX_MEDIA_DIR", "/media")),
        Path("/backups"),
    ]


def fix_ownership(env: Mapping[str, str], uid: int, gid: int) -> None:
    for d in privileges.dirs_needing_chown(data_dirs(env), uid, gid):
        log.info("entrypoint: chown -R %s:%s %s", uid, gid, d)
        try:
            subprocess.run(["chown", "-R", f"{uid}:{gid}", str(d)], check=True)
        except (OSError, subprocess.CalledProcessError) as e:
            log.warning("entrypoint: could not chown %s: %s", d, e)


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stderr)
    cmd = list(sys.argv[1:] if argv is None else argv) or list(DEFAULT_COMMAND)
    if os.geteuid() == 0:
        try:
            uid, gid = privileges.target_ids(os.environ)
        except ValueError as e:
            log.error("entrypoint: %s", e)
            sys.exit(2)
        if uid != 0:
            fix_ownership(os.environ, uid, gid)
        privileges.drop_root(uid, gid)
    os.execvp(cmd[0], cmd)


if __name__ == "__main__":
    main()
