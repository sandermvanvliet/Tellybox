"""Run as an unprivileged user inside the container (DP-3).

The image starts as root so the entrypoint can fix the ownership of bind-mounted folders,
then drops to PUID/PGID (default 1500/1500). We setuid to the numeric ids and never edit
/etc/passwd: no usermod/groupmod, and it works for any id, including ones with no account.
PUID=0 is accepted only when set explicitly (run everything as root); the default never is.
"""

from __future__ import annotations

import os
import pwd
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path

DEFAULT_ID = 1500


def _parse_id(env: Mapping[str, str], name: str) -> int:
    raw = env.get(name, "").strip()
    if not raw:
        return DEFAULT_ID
    try:
        value = int(raw)
    except ValueError:
        raise ValueError(f"{name} must be a whole number, got {raw!r}") from None
    if value < 0:
        raise ValueError(f"{name} must not be negative, got {value}")
    return value


def target_ids(env: Mapping[str, str] | None = None) -> tuple[int, int]:
    """(uid, gid) from PUID/PGID, default 1500/1500."""
    env = os.environ if env is None else env
    return _parse_id(env, "PUID"), _parse_id(env, "PGID")


def dirs_needing_chown(
    paths: Iterable[Path | str], uid: int, gid: int, stat: Callable = os.stat
) -> list[Path]:
    """Existing directories whose own owner differs. Only the top level is checked, so a
    large media volume isn't walked on every start."""
    out: list[Path] = []
    for p in map(Path, paths):
        try:
            st = stat(p)
        except OSError:
            continue
        if (st.st_mode & 0o170000) != 0o040000:
            continue
        if st.st_uid != uid or st.st_gid != gid:
            out.append(p)
    return out


def drop_root(uid: int, gid: int) -> bool:
    """Become uid:gid if running as root; a no-op otherwise. Returns whether it dropped."""
    if os.geteuid() != 0:
        return False
    os.setgroups([])
    os.setgid(gid)
    os.setuid(uid)
    try:
        home = pwd.getpwuid(uid).pw_dir
    except KeyError:
        home = "/tmp"
    os.environ["HOME"] = home
    return True
