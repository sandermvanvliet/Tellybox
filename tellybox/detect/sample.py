"""ES-4: decode a video at about 2 fps into small gray frames through an ffmpeg pipe."""

from __future__ import annotations

import collections
import subprocess
import threading
from collections.abc import Iterator
from pathlib import Path

import numpy as np

from tellybox import media_format
from tellybox.detect import SAMPLE_FILTER, SAMPLE_FPS, SAMPLE_WIDTH, Progress
from tellybox.media_format import MediaError


def frame_height(width: int, height: int) -> int:
    """The height SAMPLE_FILTER (``scale=W:-2``) gives: proportional, rounded to an even number."""
    return max(2, round(height * SAMPLE_WIDTH / width / 2) * 2)


def frames(
    video: Path, *, fps: float = SAMPLE_FPS, start_s: float = 0.0, end_s: float | None = None,
    on_progress: Progress | None = None, nice: int = 10,
) -> Iterator[tuple[float, np.ndarray]]:
    """(time in the video, normalised gray frame) at ``fps`` within [start_s, end_s).

    One ffmpeg process: ``fps=<fps>,`` + SAMPLE_FILTER, raw gray frames on stdout.
    """
    info = media_format.probe(video)
    if not info.width or not info.height:
        raise MediaError(f"no video size in {video}")
    height = frame_height(info.width, info.height)
    size = SAMPLE_WIDTH * height
    end = end_s if end_s is not None else info.duration_s
    span = (end - start_s) if end is not None else None

    cmd = ["nice", "-n", str(nice)] if nice else []
    cmd += ["ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error"]
    if start_s > 0:
        cmd += ["-ss", f"{start_s:.3f}"]
    cmd += ["-i", str(video)]
    if end_s is not None:
        cmd += ["-t", f"{max(end_s - start_s, 0.0):.3f}"]
    cmd += ["-an", "-vf", f"fps={fps:g},{SAMPLE_FILTER}", "-f", "rawvideo", "-pix_fmt", "gray", "pipe:1"]
    try:
        proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except OSError as e:
        raise MediaError(f"cannot start ffmpeg: {e}") from e

    tail: collections.deque[str] = collections.deque(maxlen=20)

    def drain() -> None:
        assert proc.stderr is not None
        for line in proc.stderr:
            if line := line.decode(errors="replace").rstrip():
                tail.append(line)

    drainer = threading.Thread(target=drain, daemon=True)
    drainer.start()
    assert proc.stdout is not None
    finished = False
    try:
        i = 0
        while True:
            buf = proc.stdout.read(size)
            if len(buf) < size:
                break
            yield start_s + i / fps, np.frombuffer(buf, dtype=np.uint8).reshape(height, SAMPLE_WIDTH)
            i += 1
            if on_progress and span and span > 0:
                on_progress(min(1.0, i / (fps * span)))
        finished = True
    finally:
        if not finished:
            proc.kill()
        proc.stdout.close()
        code = proc.wait()
        drainer.join(timeout=5)
        if proc.stderr:
            proc.stderr.close()
    if code != 0:
        raise MediaError("ffmpeg failed while sampling frames: " + " | ".join(tail))
    if on_progress:
        on_progress(1.0)
