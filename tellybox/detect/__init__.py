"""Title-card detection (v6, ES-3..ES-6, ES-9): propose cuts in a compilation from a show's profile.

The worker samples frames (``sample``), compares a region of each with the show's reference
title cards by perceptual hash (``match``), applies the episode-length hint (``hint``), snaps
each cut to a scene change or black just before the card (``snap``) and reads the title
(``ocr``). The result is only a proposal: the admin reviews every cut (ES-7).

This module holds the shared types and the hashing, so a reference marked in the admin
and a sampled frame are hashed the same way.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import imagehash
import numpy as np
from PIL import Image

HASH_SIZE = 8  # 64-bit dHash
SAMPLE_FPS = 2.0  # ES-4
SAMPLE_WIDTH = 320  # frames are scaled down to this before cropping and hashing
# Every frame, sampled or marked, is scaled by this same ffmpeg filter: a different scaler alone
# moves hashes of the same card by several bits.
SAMPLE_FILTER = f"scale={SAMPLE_WIDTH}:-2:flags=area,format=gray"
# dHash of a cropped card. Measured on synthetic cards (tests/detect_clips.py): the same card 0-1,
# a heavily degraded one 9-12, anything else 22+. pHash was unstable on mostly flat crops.
DEFAULT_THRESHOLD = 6
RESCAN_LOOSER = 8  # ES-5: a re-scan near an expected boundary accepts threshold + this
DEFAULT_SNAP_WINDOW_S = 30.0


@dataclass(frozen=True)
class Region:
    """A rectangle as fractions (0..1) of the frame, so it doesn't depend on the resolution."""

    x: float
    y: float
    w: float
    h: float

    def __post_init__(self) -> None:
        if not (0 <= self.x < 1 and 0 <= self.y < 1 and 0 < self.w <= 1 and 0 < self.h <= 1
                and self.x + self.w <= 1.0001 and self.y + self.h <= 1.0001):
            raise ValueError(f"region out of bounds: {self}")

    def crop(self, frame: np.ndarray) -> np.ndarray:
        height, width = frame.shape[:2]
        x0, y0 = int(self.x * width), int(self.y * height)
        x1, y1 = max(x0 + 1, round((self.x + self.w) * width)), max(y0 + 1, round((self.y + self.h) * height))
        return frame[y0:y1, x0:x1]

    def to_json(self) -> str:
        return json.dumps([self.x, self.y, self.w, self.h])

    @classmethod
    def from_json(cls, value: str | None) -> Region | None:
        if not value:
            return None
        x, y, w, h = (float(v) for v in json.loads(value))
        return cls(x, y, w, h)


@dataclass(frozen=True)
class Reference:
    card_hash: str  # hex dHash, from reference_hash
    region: Region | None  # None = the whole frame

    def hash(self) -> imagehash.ImageHash:
        return imagehash.hex_to_hash(self.card_hash)


@dataclass(frozen=True)
class Profile:
    references: Sequence[Reference]
    match_threshold: int = DEFAULT_THRESHOLD
    length_hint_s: float | None = None
    snap_window_s: float = DEFAULT_SNAP_WINDOW_S
    ocr: bool = False
    ocr_region: Region | None = None  # None = the whole frame (titles rarely sit inside the matched logo)


@dataclass(frozen=True)
class Hit:
    """A run of sampled frames that match a reference: one title card on screen."""

    start_s: float  # first matching frame
    end_s: float  # last matching frame
    distance: int  # best (lowest) Hamming distance in the run
    reference: int = 0  # index into Profile.references
    rescanned: bool = False  # found by the ES-5 re-scan (looser threshold), so less certain


@dataclass(frozen=True)
class Cut:
    """A proposed cut: where the next episode starts (ES-4..ES-6, ES-9)."""

    at_s: float  # the cut point, after snapping
    title_hit_s: float  # where the title card appeared
    confidence: float  # 0..1; 1 = an exact match, lower for weak matches and hint re-scans
    snapped: str | None = None  # "scene" | "black" | None (ES-6: fell back to the card)
    title: str = ""  # ES-9, empty when OCR is off or read nothing
    extra: dict = field(default_factory=dict)


Progress = Callable[[float], None]


def normalise_image(data: bytes) -> np.ndarray:
    """A still image (JPEG, PNG) as the sampler sees frames: gray, SAMPLE_WIDTH wide, same scaler."""
    out = subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error", "-i", "pipe:0", "-frames:v", "1",
         "-vf", SAMPLE_FILTER, "-f", "rawvideo", "-pix_fmt", "gray", "pipe:1"],
        input=data, capture_output=True, timeout=30,
    )
    if out.returncode != 0 or not out.stdout or len(out.stdout) % SAMPLE_WIDTH:
        raise ValueError("not an image")
    return np.frombuffer(out.stdout, dtype=np.uint8).reshape(-1, SAMPLE_WIDTH)


def hash_region(frame: np.ndarray, region: Region | None) -> imagehash.ImageHash:
    """dHash of a normalised frame's region. Sampled frames and references both go through this."""
    crop = region.crop(frame) if region else frame
    return imagehash.dhash(Image.fromarray(crop), hash_size=HASH_SIZE)


def reference_hash(image: bytes, region: Region | None) -> str:
    """ES-3: the hex hash of a marked title card (a JPEG from images.grab_frame)."""
    return str(hash_region(normalise_image(image), region))


def detect(video: Path, profile: Profile, *, on_progress: Progress | None = None) -> list[Cut]:
    """ES-4..ES-6, ES-9: the proposed cuts in ``video``, in time order.

    Sampling is 80 % of the progress, snapping and reading titles the rest.
    """
    from tellybox import images, media_format
    from tellybox.detect import hint, match, ocr, sample, snap

    refs = list(profile.references)
    if not refs:
        return []
    threshold = profile.match_threshold
    duration = media_format.probe(video).duration_s or 0.0

    def sampling(p: float) -> None:
        if on_progress:
            on_progress(0.8 * p)

    found = match.hits(sample.frames(video, on_progress=sampling), refs, threshold)

    def rescan(start_s: float, end_s: float) -> list[Hit]:
        return match.hits(sample.frames(video, fps=5.0, start_s=start_s, end_s=end_s), refs,
                          threshold + RESCAN_LOOSER)

    hits = [h for h in hint.apply(found, duration, profile.length_hint_s, rescan) if h.start_s > 1.0]
    cuts: list[Cut] = []
    for n, h in enumerate(hits):
        # Never snap back past the previous title card (short episodes, long window).
        window = min(profile.snap_window_s, h.start_s - hits[n - 1].end_s) if n else profile.snap_window_s
        at_s, kind = snap.snap(video, h.start_s, max(0.0, window))
        confidence = max(0.0, 1 - h.distance / (threshold + RESCAN_LOOSER + 1))
        if h.rescanned:
            confidence /= 2
        title = ""
        if profile.ocr and ocr.available():
            region = profile.ocr_region  # None: the whole frame
            try:
                import cv2

                jpeg = images.grab_frame(video, h.start_s + 0.5)
                gray = cv2.imdecode(np.frombuffer(jpeg, dtype=np.uint8), cv2.IMREAD_GRAYSCALE)
                title = ocr.read_title(gray, region) if gray is not None else ""
            except images.ImageError:
                title = ""
        cuts.append(Cut(at_s=at_s, title_hit_s=h.start_s, confidence=round(confidence, 3), snapped=kind,
                        title=title, extra={"distance": h.distance, "rescanned": h.rescanned}))
        if on_progress:
            on_progress(0.8 + 0.2 * (n + 1) / len(hits))
    if on_progress:
        on_progress(1.0)
    return cuts
