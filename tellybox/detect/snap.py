"""ES-6: move a cut to the scene change or black frame shortly before the title card (ffmpeg scdet, blackdetect)."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from tellybox.media_format import MediaError

PREFER_BLACK_WITHIN_S = 1.0
_BLACK = re.compile(r"black_start:\s*([0-9.]+)")
_SCENE = re.compile(r"lavfi\.scd\.time:\s*([0-9.]+)")


def snap(video: Path, hit_s: float, window_s: float) -> tuple[float, str | None]:
    """(cut time, "black" | "scene" | None). Searches [hit_s - window_s, hit_s]; the change nearest
    before the card wins, but a black start within 1 s of it is preferred (the cut lands where the
    black begins, so the previous episode ends cleanly); with none, the card itself (None)."""
    start = max(0.0, hit_s - window_s)
    span = hit_s - start
    if span <= 0:
        return hit_s, None
    cmd = ["nice", "-n", "10", "ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "info"]
    if start > 0:
        cmd += ["-ss", f"{start:.3f}"]
    cmd += ["-i", str(video), "-t", f"{span:.3f}", "-an",
            "-vf", "blackdetect=d=0.1:pix_th=0.10,scdet=threshold=10", "-f", "null", "-"]
    try:
        r = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True, text=True, errors="replace",
                           timeout=300)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise MediaError(f"snap failed: {e}") from e
    if r.returncode != 0:
        raise MediaError("ffmpeg failed while snapping: " + r.stderr.strip()[-300:])
    eps = 0.001
    blacks = [start + float(m) for m in _BLACK.findall(r.stderr)]
    scenes = [start + float(m) for m in _SCENE.findall(r.stderr)]
    blacks = [t for t in blacks if t <= hit_s + eps]
    scenes = [t for t in scenes if t <= hit_s + eps]
    if not blacks and not scenes:
        return hit_s, None
    nearest = max(blacks + scenes)
    near_blacks = [b for b in blacks if nearest - b <= PREFER_BLACK_WITHIN_S]
    if near_blacks:
        return round(max(near_blacks), 3), "black"
    return round(nearest, 3), "scene"
