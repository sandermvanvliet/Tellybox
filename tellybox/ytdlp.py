"""yt-dlp as a subprocess (CI-1, CI-2, CI-5).

yt-dlp is never imported: it runs as `python -m yt_dlp` so a newer copy can be installed
at runtime without rebuilding the image (CI-5). Updatable copies live in
`<tools_dir>/versions/<stamp>/` (pip --target layout, so executables such as deno land in
`bin/`); `<tools_dir>/current` names the active one. Without it, the yt-dlp installed in
the image is used as a fallback.

Download output (stdout, one line each, prefixed so they can't be confused with anything else):
  TBFMT <format_id>                          before download; "137+140" means two files
  TBCHAPS <json>                             chapters before download (null when none)
  TBPROG <format_id> <downloaded> <total> <total_estimate>   progress ("NA" when unknown)
  TBFILE <path>                              final file after merge/move
  TBINFO <json>                              full info dict after move
"""

from __future__ import annotations

import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from urllib.parse import parse_qs, quote, urlsplit

from tellybox import sponsorblock
from tellybox.sponsorblock import Segment

FORMAT = "bv*[height<=720][vcodec^=avc1]+ba[acodec^=mp4a]/bv*[height<=720]+ba/b[height<=720]"  # CI-2
PIP_SPEC = "yt-dlp[default,deno]"

_PROGRESS_TEMPLATE = (
    "download:TBPROG %(info.format_id)s %(progress.downloaded_bytes)s "
    "%(progress.total_bytes)s %(progress.total_bytes_estimate)s"
)

_PERMANENT = re.compile(
    r"video unavailable|private video|has been removed|members-only|join this channel"
    r"|sign in to confirm your age|unsupported url|is not a valid url|premieres in"
    r"|live event will begin|is a live stream|this video is not available",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Chapter:
    start_s: float
    end_s: float
    title: str


@dataclass(frozen=True)
class VideoInfo:
    youtube_id: str
    url: str  # webpage_url
    title: str
    channel_id: str | None
    channel_name: str | None  # 'channel' or 'uploader'
    duration_s: float | None
    thumbnail_url: str | None
    chapters: list[Chapter]
    is_live: bool


PLAYLIST_CAP = 200  # CI-7: a preview lists at most this many videos

UrlKind = Literal["video", "playlist", "both", "channel"]


def classify_url(url: str) -> UrlKind:
    """What a pasted URL points at (CI-7). Pure; no network.

    watch?v=X&list=Y (or youtu.be/X?list=Y) is "both", except YouTube Mixes (list=RD...), which are generated
    recommendations and count as "video". /playlist?list=Y is "playlist". Channel pages
    (/@name, /channel/, /c/, /user/) are "channel" (subscriptions, CS-1). Anything else,
    including youtu.be/X and non-YouTube URLs, is "video" and left to yt-dlp.
    """
    parts = _split_youtube(url)
    if parts is None:
        return "video"
    host, path, query = parts
    if host != "youtu.be":
        first = path.split("/")[1] if path.count("/") else ""
        if first.startswith("@") or first in ("channel", "c", "user"):
            return "channel"
    list_id = _list_id(query)
    if not list_id:
        return "video"
    if host == "youtu.be" or query.get("v"):
        return "video" if list_id.startswith("RD") else "both"
    return "playlist"


_YOUTUBE_HOSTS = ("youtube.com", "youtube-nocookie.com")


def _split_youtube(url: str) -> tuple[str, str, dict[str, list[str]]] | None:
    """(host, path, query) of a YouTube URL, or None for anything else. Tolerates a missing scheme."""
    url = url.strip()
    if "://" not in url:
        url = "https://" + url
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    host = (parts.hostname or "").lower()
    if host != "youtu.be" and not any(host == h or host.endswith("." + h) for h in _YOUTUBE_HOSTS):
        return None
    return host, parts.path, parse_qs(parts.query)


def _list_id(query: dict[str, list[str]]) -> str | None:
    values = query.get("list") or []
    return values[0].strip() or None if values else None


def playlist_url(playlist_id: str) -> str:
    return f"https://www.youtube.com/playlist?list={quote(playlist_id, safe='')}"


def watch_url(youtube_id: str) -> str:
    return f"https://www.youtube.com/watch?v={quote(youtube_id, safe='')}"


@dataclass(frozen=True)
class PlaylistEntry:
    youtube_id: str
    url: str  # canonical https://www.youtube.com/watch?v=<id>, never with a list parameter
    title: str
    channel_id: str | None
    channel_name: str | None
    duration_s: float | None
    thumbnail_url: str | None
    unavailable_reason: str | None  # "private", "deleted", "live", "upcoming", "unavailable"; None = can be added


@dataclass(frozen=True)
class PlaylistInfo:
    playlist_id: str
    url: str  # canonical https://www.youtube.com/playlist?list=<id>
    title: str
    channel_name: str | None
    total_count: int | None  # playlist_count from yt-dlp; may exceed len(entries) when capped
    entries: list[PlaylistEntry]  # at most PLAYLIST_CAP, in playlist order


def parse_playlist(data: dict) -> PlaylistInfo:
    """Map a `-J --flat-playlist` dict to PlaylistInfo, capped at PLAYLIST_CAP entries.

    Raises YtDlpError(retryable=False) when the dict is not a playlist.
    """
    if data.get("_type") != "playlist":
        raise YtDlpError("URL is not a playlist", retryable=False)
    pid = data.get("id")
    if not pid:
        raise YtDlpError("yt-dlp returned no playlist id", retryable=False)
    if pid == data.get("channel_id"):  # a channel page lists its uploads like a playlist
        raise YtDlpError("Channel URLs are for subscriptions, not playlists", retryable=False)
    entries = []
    for raw in data.get("entries") or []:
        if len(entries) >= PLAYLIST_CAP:
            break
        if not isinstance(raw, dict) or not raw.get("id"):
            continue
        entries.append(_playlist_entry(raw))
    count = data.get("playlist_count")
    return PlaylistInfo(
        playlist_id=pid,
        url=playlist_url(pid),
        title=data.get("title") or pid,
        channel_name=data.get("channel") or data.get("uploader"),
        total_count=int(count) if count is not None else None,
        entries=entries,
    )


_AVAILABLE = (None, "public", "unlisted")


def _playlist_entry(raw: dict) -> PlaylistEntry:
    vid = raw["id"]
    title = raw.get("title") or vid
    availability = raw.get("availability")
    live = raw.get("live_status")
    if title == "[Private video]" or availability == "private":
        reason = "private"
    elif title == "[Deleted video]":
        reason = "deleted"
    elif live == "is_live":
        reason = "live"
    elif live == "is_upcoming":
        reason = "upcoming"
    elif availability not in _AVAILABLE:  # needs_auth, subscriber_only, premium_only, ...
        reason = "unavailable"
    else:
        reason = None
    duration = raw.get("duration")
    return PlaylistEntry(
        youtube_id=vid,
        url=watch_url(vid),
        title=title,
        channel_id=raw.get("channel_id"),
        channel_name=raw.get("channel") or raw.get("uploader"),
        duration_s=float(duration) if duration is not None else None,
        thumbnail_url=_best_thumbnail(raw.get("thumbnails")) or f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg",
        unavailable_reason=reason,
    )


def _best_thumbnail(thumbnails: list[dict] | None) -> str | None:
    """URL of the largest thumbnail; flat entries list several sizes of the same image."""
    best = max(
        (t for t in thumbnails or [] if t.get("url")),
        key=lambda t: (t.get("width") or 0) * (t.get("height") or 0),
        default=None,
    )
    return best["url"] if best else None


@dataclass(frozen=True)
class DownloadResult:
    video_path: Path
    thumbnail_path: Path | None  # jpg
    info: VideoInfo  # chapters already shifted to the cut file (SB-1)
    sponsor_segments: list[Segment] = field(default_factory=list)  # removed from the file (SB-1); original timeline


@dataclass(frozen=True)
class UpdateResult:
    version: str
    previous: str | None
    changed: bool
    path: Path


class YtDlpError(Exception):
    def __init__(self, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.message = message
        self.retryable = retryable


class SponsorBlockUnavailable(YtDlpError):
    """The SponsorBlock API couldn't be reached (SB-5). Nothing was downloaded yet."""

    def __init__(self, message: str) -> None:
        super().__init__(message, retryable=True)


_SB_UNREACHABLE = "Unable to communicate with SponsorBlock API"


def _raise_for(stderr: str, returncode: int | None) -> None:
    if _SB_UNREACHABLE in stderr:
        raise SponsorBlockUnavailable(error_message(stderr, returncode))
    raise YtDlpError(error_message(stderr, returncode), retryable=classify_error(stderr))


def classify_error(stderr: str) -> bool:
    """True = retryable. Unknown errors are retryable (YouTube changes; an update may fix it)."""
    return not _PERMANENT.search(stderr)


def error_message(stderr: str, returncode: int | None = None) -> str:
    lines = [ln.strip() for ln in stderr.splitlines() if ln.strip()]
    errors = [ln for ln in lines if ln.startswith("ERROR:")]
    if errors:
        return errors[-1].removeprefix("ERROR:").strip()
    if lines:
        return lines[-1]
    return f"yt-dlp exited with code {returncode}"


def parse_info(data: dict) -> VideoInfo:
    """Map a yt-dlp info dict to VideoInfo. Raises YtDlpError(retryable=False) for non-videos."""
    if data.get("_type") in ("playlist", "multi_video"):
        raise YtDlpError("URL is a playlist, not a single video", retryable=False)
    vid = data.get("id")
    if not vid:
        raise YtDlpError("yt-dlp returned no video id", retryable=False)
    chapters = [
        Chapter(float(c.get("start_time") or 0), float(c.get("end_time") or 0), c.get("title") or "")
        for c in data.get("chapters") or []
    ]
    duration = data.get("duration")
    return VideoInfo(
        youtube_id=vid,
        url=data.get("webpage_url") or data.get("original_url") or "",
        title=data.get("title") or vid,
        channel_id=data.get("channel_id"),
        channel_name=data.get("channel") or data.get("uploader"),
        duration_s=float(duration) if duration is not None else None,
        thumbnail_url=data.get("thumbnail"),
        chapters=chapters,
        is_live=bool(data.get("is_live")) or data.get("live_status") in ("is_live", "is_upcoming"),
    )


def _num(s: str) -> float | None:
    try:
        return float(s)
    except ValueError:
        return None  # "NA"


class _Progress:
    """Combines per-file progress (video + audio download separately) into one monotonic 0..1."""

    def __init__(self, cb: Callable[[float], None] | None) -> None:
        self.cb, self.files, self.parts, self.last = cb, {}, 1, -1.0

    def formats(self, format_id: str) -> None:
        self.parts = max(1, len(format_id.split("+")))

    def update(self, format_id: str, done: float | None, total: float | None) -> None:
        if done is None or not total:
            return
        self.files[format_id] = max(self.files.get(format_id, 0.0), min(1.0, done / total))
        self.report(sum(self.files.values()) / max(self.parts, len(self.files)))

    def report(self, p: float) -> None:
        if self.cb and (p - self.last >= 0.01 or (p >= 1.0 > self.last)):
            self.last = p
            self.cb(p)


class YtDlp:
    def __init__(self, tools_dir: Path, *, python: str = sys.executable) -> None:
        self.tools_dir = Path(tools_dir)
        self.python = python

    def active_dir(self) -> Path | None:
        return _active_dir(self.tools_dir)

    def command(self) -> list[str]:
        return [self.python, "-m", "yt_dlp"]

    def env(self) -> dict[str, str]:
        return _env(self.active_dir(), self.python)

    def _run(self, args: list[str], timeout: float) -> str:
        try:
            p = subprocess.run(
                [*self.command(), *args], env=self.env(), capture_output=True, text=True,
                timeout=timeout, stdin=subprocess.DEVNULL,
            )
        except subprocess.TimeoutExpired:
            raise YtDlpError(f"yt-dlp timed out after {timeout:g}s", retryable=True) from None
        if p.returncode != 0:
            _raise_for(p.stderr, p.returncode)
        return p.stdout

    def version(self) -> str:
        return self._run(["--version"], 60).strip()

    def preview(self, url: str, *, timeout: float = 90) -> VideoInfo:
        """CI-1: metadata only, shown to the admin before download."""
        out = self._run(["-J", "--no-playlist", "--skip-download", "--", url], timeout)
        try:
            info = parse_info(json.loads(out))
        except json.JSONDecodeError:
            raise YtDlpError("yt-dlp returned invalid JSON", retryable=True) from None
        if info.is_live:
            raise YtDlpError("Live streams and premieres can't be downloaded", retryable=False)
        return info

    def preview_playlist(self, url: str, *, timeout: float = 120) -> PlaylistInfo:
        """CI-7: flat listing of a playlist (one call), capped at PLAYLIST_CAP entries.

        A watch?v=X&list=Y URL lists playlist Y: the admin chose "whole playlist".
        """
        if classify_url(url) == "channel":
            raise YtDlpError("Channel URLs are for subscriptions, not playlists", retryable=False)
        parts = _split_youtube(url)
        list_id = _list_id(parts[2]) if parts else None
        if list_id:
            url = playlist_url(list_id)
        out = self._run(
            ["-J", "--flat-playlist", "--skip-download", "--playlist-end", str(PLAYLIST_CAP), "--", url], timeout
        )
        try:
            data = json.loads(out)
        except json.JSONDecodeError:
            raise YtDlpError("yt-dlp returned invalid JSON", retryable=True) from None
        return parse_playlist(data)

    def sponsor_segments(self, url: str, categories: list[str], *, timeout: float = 120) -> list[Segment]:
        """SB-3: the video's current SponsorBlock segments in `categories`, without downloading.

        Raises SponsorBlockUnavailable when the API can't be reached, YtDlpError otherwise.
        """
        if not categories:
            return []
        out = self._run(
            ["--simulate", "--no-playlist", "--sponsorblock-mark", ",".join(categories),
             "--print", "TBSB %(sponsorblock_chapters)j", "--", url],
            timeout,
        )
        for line in out.splitlines():
            tag, _, rest = line.partition(" ")
            if tag == "TBSB":
                if rest.strip() == "NA":  # not a YouTube video: SponsorBlock doesn't apply
                    return []
                try:
                    chapters = json.loads(rest)
                except json.JSONDecodeError:
                    raise YtDlpError("yt-dlp returned invalid JSON", retryable=True) from None
                return sponsorblock.segments_from_info(chapters, categories)
        raise YtDlpError("yt-dlp reported no SponsorBlock result", retryable=True)

    def download(
        self, url: str, dest_dir: Path, *,
        on_progress: Callable[[float], None] | None = None, timeout: float = 4 * 3600,
        sb_categories: list[str] | None = None,
    ) -> DownloadResult:
        """CI-2: download at most 720p, preferring H.264 + AAC so processing can remux.

        SB-1: with `sb_categories`, their segments are cut out (at keyframes).
        Raises SponsorBlockUnavailable, before anything is downloaded, when the API can't be
        reached; the caller downloads again without (SB-5).
        """
        dest = Path(dest_dir).resolve()
        dest.mkdir(parents=True, exist_ok=True)
        sb_args = ["--sponsorblock-remove", ",".join(sb_categories)] if sb_categories else []
        args = [
            "-f", FORMAT, "--no-playlist", "--newline", "--no-mtime", "--progress",
            "--write-thumbnail", "--convert-thumbnails", "jpg", *sb_args,
            "-o", f"{dest}/video.%(ext)s", "-o", f"thumbnail:{dest}/thumb.%(ext)s",
            "--progress-template", _PROGRESS_TEMPLATE,
            "--print", "before_dl:TBFMT %(format_id)s",
            "--print", "before_dl:TBCHAPS %(chapters)j",
            "--print", "after_move:TBFILE %(filepath)s",
            "--print", "after_move:TBINFO %()j",
            "--", url,
        ]
        proc = subprocess.Popen(
            [*self.command(), *args], env=self.env(), stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1,
        )
        lines: queue.Queue[str | None] = queue.Queue()
        stderr: list[str] = []

        def pump_out() -> None:
            for line in proc.stdout:
                lines.put(line)
            lines.put(None)

        def pump_err() -> None:
            stderr.extend(proc.stderr)

        threads = [threading.Thread(target=f, daemon=True) for f in (pump_out, pump_err)]
        for t in threads:
            t.start()
        progress = _Progress(on_progress)
        filepath: str | None = None
        info: dict | None = None
        had_chapters = True
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            try:
                if remaining <= 0:
                    raise queue.Empty
                line = lines.get(timeout=remaining)
            except queue.Empty:
                proc.kill()
                proc.wait()
                raise YtDlpError(f"yt-dlp timed out after {timeout:g}s", retryable=True) from None
            if line is None:
                break
            tag, _, rest = line.rstrip("\r\n").partition(" ")
            if tag == "TBPROG":
                fmt, done, total, est = (rest.split(" ") + ["NA"] * 4)[:4]
                progress.update(fmt, _num(done), _num(total) or _num(est))
            elif tag == "TBFMT":
                progress.formats(rest)
            elif tag == "TBCHAPS":
                had_chapters = rest not in ("null", "NA", "[]")
            elif tag == "TBFILE":
                filepath = rest
            elif tag == "TBINFO":
                info = json.loads(rest)
        try:
            proc.wait(timeout=max(1.0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            raise YtDlpError(f"yt-dlp timed out after {timeout:g}s", retryable=True) from None
        for t in threads:
            t.join(timeout=5)
        err = "".join(stderr)
        if proc.returncode != 0:
            _raise_for(err, proc.returncode)
        if not filepath or info is None or not Path(filepath).is_file():
            raise YtDlpError("yt-dlp finished without reporting the downloaded file", retryable=True)
        progress.report(1.0)
        thumb = dest / "thumb.jpg"
        segments = sponsorblock.segments_from_info(info.get("sponsorblock_chapters"), sb_categories or [])
        if segments and not had_chapters:
            info["chapters"] = None  # ModifyChapters makes up one chapter for the whole video
        return DownloadResult(Path(filepath), thumb if thumb.is_file() else None, parse_info(info), segments)


def _active_dir(tools_dir: Path) -> Path | None:
    try:
        name = (tools_dir / "current").read_text().strip()
    except OSError:
        return None
    path = tools_dir / "versions" / name
    return path if name and path.is_dir() else None


def _env(active: Path | None, python: str) -> dict[str, str]:
    env = dict(os.environ)
    path = env.get("PATH", os.defpath)
    # The fallback (image) install puts deno next to the interpreter; make sure it's found
    # even when the venv isn't activated.
    path = os.pathsep.join([path, str(Path(python).parent)])
    if active:
        env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(active), env.get("PYTHONPATH")]))
        path = os.pathsep.join([str(active / "bin"), path])
    env["PATH"] = path
    return env


def _pip_installer(python: str) -> Callable[[Path], None]:
    def install(target: Path) -> None:
        p = subprocess.run(
            [python, "-m", "pip", "install", "--no-cache-dir", "--upgrade", "--disable-pip-version-check",
             "--target", str(target), PIP_SPEC],
            capture_output=True, text=True, timeout=900, stdin=subprocess.DEVNULL,
        )
        if p.returncode != 0:
            raise YtDlpError(f"pip install failed: {error_message(p.stderr, p.returncode)}", retryable=True)

    return install


def update(
    tools_dir: Path, *, python: str = sys.executable, installer: Callable[[Path], None] | None = None,
) -> UpdateResult:
    """CI-5: install the latest yt-dlp next to the active one and switch only if it works."""
    tools_dir = Path(tools_dir)
    versions = tools_dir / "versions"
    versions.mkdir(parents=True, exist_ok=True)
    old = _active_dir(tools_dir)
    previous = _version_in(old, python) if old else None

    new = versions / datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    new.mkdir()
    try:
        (installer or _pip_installer(python))(new)
        if not (new / "yt_dlp" / "__init__.py").is_file():
            raise YtDlpError("update did not install the yt_dlp package", retryable=True)
        version = _version_in(new, python)
        if not version:
            raise YtDlpError("new yt-dlp failed to report its version", retryable=True)
    except YtDlpError:
        shutil.rmtree(new, ignore_errors=True)
        raise
    except Exception as e:
        shutil.rmtree(new, ignore_errors=True)
        raise YtDlpError(f"yt-dlp update failed: {e}", retryable=True) from e

    if old and version == previous:
        shutil.rmtree(new, ignore_errors=True)
        return UpdateResult(version, previous, False, old)

    tmp = tools_dir / f"current.{os.getpid()}.tmp"
    tmp.write_text(new.name + "\n")
    os.replace(tmp, tools_dir / "current")
    keep = {new.name, old.name if old else None}
    for d in versions.iterdir():
        if d.name not in keep:
            shutil.rmtree(d, ignore_errors=True)
    return UpdateResult(version, previous, True, new)


def _version_in(dir: Path, python: str) -> str | None:
    try:
        p = subprocess.run(
            [python, "-m", "yt_dlp", "--version"], env=_env(dir, python), capture_output=True,
            text=True, timeout=60, stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return p.stdout.strip() or None if p.returncode == 0 else None
