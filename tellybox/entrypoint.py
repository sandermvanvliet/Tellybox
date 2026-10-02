"""Container entrypoint: `python -m tellybox.entrypoint [CMD...]` (DP-3).

Not root (compose `user:` or `docker run --user`): exec the command unchanged.
Root: chown the data folders if needed, drop to PUID/PGID, exec the command.
"""

from __future__ import annotations

import json
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


# Home Assistant add-on options (DP-7) -> environment variables.
OPTION_ENV = {
    "web_port": "TELLYBOX_WEB_PORT",
    "cast_api_port": "TELLYBOX_CAST_API_PORT",
    "media_base_url": "TELLYBOX_MEDIA_BASE_URL",
    "admin_password": "TELLYBOX_ADMIN_PASSWORD",
}


def options_to_env(options: dict, env: Mapping[str, str]) -> dict[str, str]:
    """Env vars to add for the given add-on options. Variables already set win; empty values
    and unknown options are skipped."""
    out: dict[str, str] = {}
    for key, name in OPTION_ENV.items():
        value = options.get(key)
        if value is None or isinstance(value, (dict, list)):
            continue
        value = str(value).strip()
        if value and name not in env:
            out[name] = value
    return out


def apply_options_file(env: dict) -> None:
    """If TELLYBOX_OPTIONS_FILE points at an existing JSON file, fill `env` from it."""
    path = env.get("TELLYBOX_OPTIONS_FILE")
    if not path or not Path(path).is_file():
        return
    try:
        options = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(options, dict):
            raise ValueError("not a JSON object")
    except (OSError, ValueError) as e:
        log.warning("entrypoint: ignoring options file %s: %s", path, e)
        return
    env.update(options_to_env(options, env))


def create_dirs(env: Mapping[str, str]) -> None:
    """mkdir -p the data and media folders (not /backups) so the ownership check sees them."""
    for d in data_dirs(env)[:2]:
        try:
            d.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            log.warning("entrypoint: could not create %s: %s", d, e)


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
    apply_options_file(os.environ)  # type: ignore[arg-type]
    if os.geteuid() == 0:
        try:
            uid, gid = privileges.target_ids(os.environ)
        except ValueError as e:
            log.error("entrypoint: %s", e)
            sys.exit(2)
        create_dirs(os.environ)
        if uid != 0:
            fix_ownership(os.environ, uid, gid)
        privileges.drop_root(uid, gid)
    os.execvp(cmd[0], cmd)


if __name__ == "__main__":
    main()
