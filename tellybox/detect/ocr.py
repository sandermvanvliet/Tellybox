"""ES-9 (optional): read the episode title from the title card with Tesseract."""

from __future__ import annotations

import shutil

import numpy as np

from tellybox.detect import Region

LANGUAGES = "eng+nld+deu"  # the image installs these (Dockerfile)


def available() -> bool:
    """Whether the tesseract binary is installed; without it, titles are simply not read."""
    return shutil.which("tesseract") is not None


def read_title(frame: np.ndarray, region: Region | None) -> str:
    """The cleaned-up title text in ``region`` of a full-resolution frame, or "" (also when unavailable). Slice A."""
    raise NotImplementedError
