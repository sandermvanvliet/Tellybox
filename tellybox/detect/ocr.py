"""ES-9 (optional): read the episode title from the title card with Tesseract."""

from __future__ import annotations

import re
import shutil

import numpy as np

from tellybox.detect import Region

LANGUAGES = "eng+nld+deu"  # the image installs these (Dockerfile)


def available() -> bool:
    """Whether the tesseract binary is installed; without it, titles are simply not read."""
    return shutil.which("tesseract") is not None


def read_title(frame: np.ndarray, region: Region | None) -> str:
    """The cleaned-up title text in ``region`` of a full-resolution gray frame, or "" (also when unavailable)."""
    if not available():
        return ""
    import cv2
    import pytesseract

    crop = region.crop(frame) if region else frame
    if crop.size == 0:
        return ""
    if crop.ndim == 3:
        crop = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    crop = cv2.resize(crop, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
    _, binary = cv2.threshold(crop, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    # Tesseract wants dark text on a light background; a title card is usually the reverse.
    if binary.mean() < 127:
        binary = 255 - binary
    try:
        text = pytesseract.image_to_string(binary, lang=LANGUAGES, config="--psm 7")
    except (pytesseract.TesseractError, OSError):
        return ""
    text = re.sub(r"[^\w\s.,:;!?'&()\-]", "", text).replace("_", "")
    text = re.sub(r"\s+", " ", text).strip()
    return text if sum(c.isalpha() for c in text) >= 3 else ""
