"""Show artwork and episode thumbnails: frame grabs and upload clean-up (LM-2).

ffmpeg always runs as a subprocess with a fixed argument list and a timeout; no
user-supplied text ever reaches a shell. Uploads are validated by magic bytes,
never by filename or the browser's Content-Type header, and re-encoded so a
crafted or oversized file can't reach disk unexamined.

Saved files get a fresh name each time (a monotonic stamp), so a browser that
cached the old artwork or thumbnail by URL sees the replacement immediately.
"""

from __future__ import annotations

import itertools
import os
import subprocess
import tempfile
import time
from pathlib import Path

from tellybox import media_format
from tellybox.i18n import _
from tellybox.media_format import MediaError

MAX_WIDTH = 1280
MAX_UPLOAD_BYTES = 5 * 1024 * 1024
GRAB_TIMEOUT_S = 20
ENCODE_TIMEOUT_S = 20
FILE_MODE = 0o644

_SCALE = f"scale='min({MAX_WIDTH},iw)':-2"  # cap width, never upscale, keep aspect, even dimensions
_counter = itertools.count()


class ImageError(Exception):
    """A user-facing message (in the request's language): a bad upload, or ffmpeg could not produce an image."""


def _sniff(data: bytes) -> str | None:
    """Image kind by magic bytes, or None if it's none of JPEG/PNG/WebP."""
    if data[:3] == b"\xff\xd8\xff":
        return "jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None


def _run_ffmpeg(cmd: list[str], *, input_data: bytes | None, timeout: float) -> bytes:
    try:
        result = subprocess.run(cmd, input=input_data, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise ImageError(_("could not process the image or video")) from e
    if result.returncode != 0 or not result.stdout:
        tail = (result.stderr or b"").decode(errors="replace").strip().splitlines()[-5:]
        raise ImageError(
            _("ffmpeg could not produce an image: %(detail)s") % {"detail": " / ".join(tail)} if tail else _("ffmpeg failed")
        )
    return result.stdout


def grab_frame(video: Path, at_s: float) -> bytes:
    """One JPEG frame from `video` at `at_s`, at most MAX_WIDTH px wide (LM-2). `at_s` is clamped into range."""
    video = Path(video)
    try:
        info = media_format.probe(video)
    except MediaError as e:
        raise ImageError(_("cannot read that video: %(error)s") % {"error": e}) from e
    at = max(0.0, at_s)
    if info.duration_s:
        # A fast (keyframe-seeking) -ss right at the last frame can miss it and yield nothing,
        # so stay at least two frame intervals clear of the end.
        margin = max(2.0 / (info.fps or 25.0), 0.1)
        at = min(at, max(info.duration_s - margin, 0.0))
    cmd = [
        "ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error", "-y",
        "-ss", f"{at:.3f}", "-i", str(video),
        "-frames:v", "1", "-vf", f"{_SCALE},format=yuvj420p", "-f", "image2", "-vcodec", "mjpeg", "pipe:1",
    ]
    return _run_ffmpeg(cmd, input_data=None, timeout=GRAB_TIMEOUT_S)


def clean_upload(data: bytes) -> bytes:
    """Accept a JPEG/PNG/WebP of at most 5 MB; re-encode to a JPEG at most MAX_WIDTH px wide (LM-2).

    Raises ImageError with a user-facing message for anything else, or if ffmpeg fails.
    """
    if len(data) > MAX_UPLOAD_BYTES:
        raise ImageError(_("image is too large (max 5 MB)"))
    if _sniff(data) is None:
        raise ImageError(_("file is not a JPEG, PNG or WebP image"))
    cmd = [
        "ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error", "-y",
        "-i", "pipe:0",
        "-frames:v", "1", "-vf", f"{_SCALE},format=yuvj420p", "-f", "image2", "-vcodec", "mjpeg", "pipe:1",
    ]
    return _run_ffmpeg(cmd, input_data=data, timeout=ENCODE_TIMEOUT_S)


def _fresh_name(prefix: str, id_: int) -> str:
    return f"{prefix}-{id_}-{int(time.time())}{next(_counter):04d}.jpg"


def _save(media_dir: Path, subdir: str, name: str, data: bytes) -> str:
    """Write `data` atomically under media_dir/subdir/name, mode 0o644; return the relative path."""
    directory = media_dir / subdir
    directory.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{name}.", suffix=".part", dir=directory)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.chmod(tmp_name, FILE_MODE)
        os.replace(tmp_name, directory / name)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise
    return f"{subdir}/{name}"


def save_show_artwork(media_dir: Path, show_id: int, data: bytes) -> str:
    return _save(media_dir, "art", _fresh_name("show", show_id), data)


def save_episode_thumbnail(media_dir: Path, episode_id: int, data: bytes) -> str:
    return _save(media_dir, "thumbs", _fresh_name("episode", episode_id), data)


def save_profile_photo(media_dir: Path, profile_id: int, data: bytes) -> str:
    """A kid profile's photo (PR-1), under media/profiles/."""
    return _save(media_dir, "profiles", _fresh_name("profile", profile_id), data)


def save_split_reference(media_dir: Path, show_id: int, data: bytes) -> str:
    """A title card marked for a show's splitting profile (ES-3), under media/split/."""
    return _save(media_dir, "split", _fresh_name("show", show_id), data)
