"""ES-6: move a cut to the scene change or black frame shortly before the title card (ffmpeg scdet, blackdetect)."""

from __future__ import annotations

from pathlib import Path


def snap(video: Path, hit_s: float, window_s: float) -> tuple[float, str | None]:
    """(cut time, "black" | "scene" | None). Searches [hit_s - window_s, hit_s]; the change nearest
    before the card wins; with none, the card itself (None). Slice A."""
    raise NotImplementedError
