"""Probe media and convert it to the format the 1st-gen Chromecast plays (CI-2, NF-6, NF-8).

Target (CLAUDE.md "Environment"): MP4 with faststart (moov before mdat), H.264
Baseline/Constrained Baseline/Main/High at level <= 4.1, yuv420p, height <= 720, and
AAC audio with at most 2 channels. Existing compliant H.264/AAC is remuxed rather than
re-encoded (CI-2); encodes use x264 veryfast (NF-6) at low CPU priority. Output is
written to a temp file and moved into place only on success (NF-8).

CLI: ``python -m tellybox.media_format {probe,verify} FILE``.
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path

ALLOWED_PROFILES = frozenset({"Baseline", "Constrained Baseline", "Main", "High"})
MAX_LEVEL = 41
MAX_HEIGHT = 720
MAX_AUDIO_CHANNELS = 2
TARGET_PIX_FMT = "yuv420p"
STDERR_TAIL = 15


class MediaError(Exception):
    pass


@dataclass(frozen=True)
class StreamInfo:
    duration_s: float | None
    video_codec: str | None
    video_profile: str | None
    video_level: int | None  # ffprobe level, e.g. 40, 41, 31
    pix_fmt: str | None
    width: int | None
    height: int | None
    fps: float | None
    audio_codec: str | None
    audio_channels: int | None
    faststart: bool | None  # moov before mdat; None if not an MP4/MOV container


@dataclass(frozen=True)
class Plan:
    copy_video: bool
    copy_audio: bool  # meaningful only when has_audio
    scale_to_720: bool
    has_audio: bool

    @property
    def remux_only(self) -> bool:
        return self.copy_video and (self.copy_audio or not self.has_audio)


# --- probing ------------------------------------------------------------------------------------


def _float(v: object) -> float | None:
    try:
        f = float(v)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return f if f == f and f >= 0 else None  # drop NaN / negatives


def _rate(v: object) -> float | None:
    if not isinstance(v, str) or "/" not in v:
        return _float(v)
    num, den = v.split("/", 1)
    n, d = _float(num), _float(den)
    return n / d if n and d else None


def _int(v: object) -> int | None:
    try:
        i = int(v)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return i if i > 0 else None  # ffprobe reports unknown levels as -99


def moov_before_mdat(path: Path) -> bool:
    """True if the top-level moov atom precedes mdat (faststart, CI-2)."""
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        end = f.tell()
        pos = 0
        while pos + 8 <= end:
            f.seek(pos)
            size, kind = struct.unpack(">I4s", f.read(8))
            header = 8
            if size == 1:
                raw = f.read(8)
                if len(raw) < 8:
                    return False
                size = struct.unpack(">Q", raw)[0]
                header = 16
            elif size == 0:
                size = end - pos  # atom runs to end of file
            if kind == b"moov":
                return True
            if kind == b"mdat" or size < header:
                return False
            pos += size
    return False


def probe(path: Path) -> StreamInfo:
    """Read the first video and first audio stream with ffprobe. MediaError if unreadable or no video."""
    path = Path(path)
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
            capture_output=True, text=True, timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        raise MediaError(f"ffprobe failed on {path}: {e}") from e
    if r.returncode != 0:
        raise MediaError(f"cannot read {path}: {r.stderr.strip() or 'ffprobe failed'}")
    try:
        data = json.loads(r.stdout or "{}")
    except json.JSONDecodeError as e:
        raise MediaError(f"cannot parse ffprobe output for {path}") from e

    streams = data.get("streams") or []
    fmt = data.get("format") or {}
    video = next((s for s in streams if s.get("codec_type") == "video"
                  and not (s.get("disposition") or {}).get("attached_pic")), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    if video is None:
        raise MediaError(f"no video stream in {path}")

    is_mp4 = "mp4" in (fmt.get("format_name") or "").split(",") or "mov" in (fmt.get("format_name") or "").split(",")
    faststart: bool | None = None
    if is_mp4:
        try:
            faststart = moov_before_mdat(path)
        except OSError:
            faststart = None

    return StreamInfo(
        duration_s=_float(fmt.get("duration")) or _float(video.get("duration")),
        video_codec=video.get("codec_name"),
        video_profile=video.get("profile"),
        video_level=_int(video.get("level")),
        pix_fmt=video.get("pix_fmt"),
        width=_int(video.get("width")),
        height=_int(video.get("height")),
        fps=_rate(video.get("avg_frame_rate")) or _rate(video.get("r_frame_rate")),
        audio_codec=audio.get("codec_name") if audio else None,
        audio_channels=_int(audio.get("channels")) if audio else None,
        faststart=faststart,
    )


# --- planning -----------------------------------------------------------------------------------


def _video_ok(info: StreamInfo) -> bool:
    return (
        info.video_codec == "h264"
        and info.video_profile in ALLOWED_PROFILES
        and info.video_level is not None
        and info.video_level <= MAX_LEVEL
        and info.pix_fmt == TARGET_PIX_FMT
        and info.height is not None
        and info.height <= MAX_HEIGHT
    )


def _audio_ok(info: StreamInfo) -> bool:
    return info.audio_codec == "aac" and (info.audio_channels or 0) <= MAX_AUDIO_CHANNELS


def plan_for(info: StreamInfo) -> Plan:
    """Remux compliant streams, re-encode the rest (CI-2). Missing faststart alone needs only a remux."""
    has_audio = info.audio_codec is not None
    return Plan(
        copy_video=_video_ok(info),
        copy_audio=has_audio and _audio_ok(info),
        scale_to_720=info.height is not None and info.height > MAX_HEIGHT,
        has_audio=has_audio,
    )


# --- conversion ---------------------------------------------------------------------------------


def _ffmpeg_cmd(
    src: Path, tmp: Path, plan: Plan, nice: int, span: tuple[float, float] | None = None
) -> list[str]:
    """``span`` = (start_s, length_s) encodes only that part. Seeking before ``-i`` while
    re-encoding decodes from the previous keyframe and drops up to the start, so the
    part starts on the exact frame (ES-8)."""
    cmd: list[str] = []
    if nice > 0 and (nice_bin := shutil.which("nice")):
        cmd += [nice_bin, "-n", str(nice)]
    cmd += ["ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "warning", "-y"]
    if span is not None:
        cmd += ["-ss", f"{span[0]:.3f}"]
    cmd += ["-i", str(src)]
    if span is not None:
        cmd += ["-t", f"{span[1]:.3f}"]
    cmd += [
        "-map", "0:v:0", "-map", "0:a:0?", "-sn", "-dn",
    ]
    if plan.copy_video:
        cmd += ["-c:v", "copy"]
    else:
        # NF-6: x264 veryfast. Scale to 720p when taller; otherwise just force even dimensions
        # (yuv420p needs them).
        vf = "scale=-2:720" if plan.scale_to_720 else "scale=trunc(iw/2)*2:trunc(ih/2)*2"
        cmd += [
            "-vf", vf,
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
            "-profile:v", "high", "-level:v", "4.1", "-pix_fmt", "yuv420p",
        ]
    if plan.copy_audio:
        cmd += ["-c:a", "copy"]
    else:
        cmd += ["-c:a", "aac", "-b:a", "128k", "-ac", "2"]
    cmd += ["-movflags", "+faststart", "-f", "mp4", "-progress", "pipe:1", "-nostats", str(tmp)]
    return cmd


def convert(
    src: Path,
    dst: Path,
    plan: Plan,
    *,
    duration_s: float | None = None,
    on_progress: Callable[[float], None] | None = None,
    nice: int = 10,
    timeout: float | None = None,
) -> None:
    """Convert ``src`` to a target-format MP4 at ``dst`` following ``plan``.

    Writes to a temp file next to ``dst`` and renames it into place only on success, so a
    failure never leaves a partial ``dst`` (NF-8). A pre-existing ``dst`` is left untouched
    on failure. Raises MediaError with the tail of ffmpeg's stderr.
    """
    src, dst = Path(src), Path(dst)
    if duration_s is None and on_progress is not None:
        try:
            duration_s = probe(src).duration_s
        except MediaError:
            duration_s = None

    dst.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{dst.name}.", suffix=".part", dir=dst.parent)
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        _run_ffmpeg(_ffmpeg_cmd(src, tmp, plan, nice), duration_s, on_progress, timeout)
        os.replace(tmp, dst)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def cut(
    src: Path,
    dst: Path,
    start_s: float,
    end_s: float,
    *,
    on_progress: Callable[[float], None] | None = None,
    nice: int = 10,
    timeout: float | None = None,
) -> None:
    """Encode ``[start_s, end_s)`` of ``src`` into a target-format MP4 at ``dst`` (ES-8).

    Always re-encodes (frame-accurate start and end), with the same settings as ``convert``;
    taller than 720p is scaled down. Same temp-file and failure behaviour as ``convert``.
    Doesn't verify; the caller runs ``verify`` on the result.
    """
    src, dst = Path(src), Path(dst)
    if end_s - start_s <= 0:
        raise MediaError(f"empty part {start_s:.3f}..{end_s:.3f}")
    info = probe(src)
    plan = Plan(
        copy_video=False,
        copy_audio=False,
        scale_to_720=info.height is not None and info.height > MAX_HEIGHT,
        has_audio=info.audio_codec is not None,
    )
    length_s = end_s - start_s
    dst.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{dst.name}.", suffix=".part", dir=dst.parent)
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        _run_ffmpeg(_ffmpeg_cmd(src, tmp, plan, nice, (start_s, length_s)), length_s, on_progress, timeout)
        os.replace(tmp, dst)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _run_ffmpeg(
    cmd: list[str],
    duration_s: float | None,
    on_progress: Callable[[float], None] | None,
    timeout: float | None,
) -> None:
    try:
        proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, errors="replace")
    except OSError as e:
        raise MediaError(f"cannot start ffmpeg: {e}") from e

    tail: collections.deque[str] = collections.deque(maxlen=STDERR_TAIL)

    def drain_stderr() -> None:
        assert proc.stderr is not None
        for line in proc.stderr:
            if line := line.rstrip():
                tail.append(line)

    timed_out = threading.Event()

    def kill() -> None:
        timed_out.set()
        proc.kill()

    err_thread = threading.Thread(target=drain_stderr, daemon=True)
    err_thread.start()
    timer = threading.Timer(timeout, kill) if timeout is not None else None
    if timer:
        timer.daemon = True
        timer.start()

    last = 0.0
    finished = False

    def report(frac: float) -> None:
        nonlocal last
        frac = min(1.0, max(0.0, frac))
        if on_progress and frac > last and (frac - last >= 0.01 or frac == 1.0):
            last = frac
            on_progress(frac)

    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            key, _, value = line.strip().partition("=")
            if key == "out_time_us" and duration_s:
                us = _float(value)
                if us is not None:
                    report(us / 1e6 / duration_s)
            elif key == "progress" and value == "end":
                finished = True
        proc.wait()
    finally:
        if timer:
            timer.cancel()
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        err_thread.join(timeout=5)

    if timed_out.is_set():
        raise MediaError(f"ffmpeg timed out after {timeout} s\n" + "\n".join(tail))
    if proc.returncode != 0:
        raise MediaError(f"ffmpeg exited with {proc.returncode}\n" + "\n".join(tail))
    if finished or not duration_s:
        report(1.0)


# --- verification -------------------------------------------------------------------------------


def violations(info: StreamInfo) -> list[str]:
    """Every way ``info`` misses the Chromecast target format (empty if compliant)."""
    out: list[str] = []
    if info.faststart is None:
        out.append("container is not MP4")
    elif not info.faststart:
        out.append("no faststart (moov after mdat)")
    if info.video_codec != "h264":
        out.append(f"video codec {info.video_codec}, need h264")
    else:
        if info.video_profile not in ALLOWED_PROFILES:
            out.append(f"H.264 profile {info.video_profile}, need one of {', '.join(sorted(ALLOWED_PROFILES))}")
        if info.video_level is None or info.video_level > MAX_LEVEL:
            out.append(f"H.264 level {info.video_level}, need <= {MAX_LEVEL}")
    if info.pix_fmt != TARGET_PIX_FMT:
        out.append(f"pixel format {info.pix_fmt}, need {TARGET_PIX_FMT}")
    if info.height is None or info.height > MAX_HEIGHT:
        out.append(f"height {info.height}, need <= {MAX_HEIGHT}")
    if info.audio_codec is not None:
        if info.audio_codec != "aac":
            out.append(f"audio codec {info.audio_codec}, need aac")
        if (info.audio_channels or 0) > MAX_AUDIO_CHANNELS:
            out.append(f"{info.audio_channels} audio channels, need <= {MAX_AUDIO_CHANNELS}")
    return out


def verify(path: Path) -> StreamInfo:
    """Probe ``path`` and check it is in the target format; MediaError lists every violation."""
    info = probe(path)
    if problems := violations(info):
        raise MediaError(f"{path} is not in the target format: " + "; ".join(problems))
    return info


# --- CLI ----------------------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m tellybox.media_format",
                                     description="Probe media or verify the Chromecast target format.")
    parser.add_argument("command", choices=["probe", "verify"])
    parser.add_argument("file", type=Path)
    args = parser.parse_args(argv)
    try:
        info = verify(args.file) if args.command == "verify" else probe(args.file)
    except MediaError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    for key, value in asdict(info).items():
        print(f"{key}: {value}")
    if args.command == "verify":
        print(f"ok: {args.file}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
