"""Synthetic compilations for title-card detection tests (v6): known title cards at known times.

Each episode is [black][title card][content]. The card is a navy frame with a yellow logo box
(top left) and, when a font is found, the text "Episode n" in the middle (for OCR). Content
alternates between lavfi sources that look nothing like the card. A "weak" card is the same
card with heavy noise and darker, so it matches only with a looser threshold (ES-5 re-scan).
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

SIZE = "640x360"
RATE = 25
CONTENT = ("testsrc2", "mandelbrot", "cellauto=rule=110", "life=mold=10:ratio=0.3", "rgbtestsrc", "smptebars")
LOGO_REGION = [0.05, 0.08, 0.30, 0.25]  # the yellow box, as fractions of the frame


@dataclass(frozen=True)
class Compilation:
    path: Path
    duration_s: float
    card_starts: list[float]  # where each episode's title card appears
    black_starts: list[float]  # where the black before each card starts (the ideal cut, ES-6)
    reference: bytes  # a JPEG of the first card, as the admin would mark it
    titles: list[str]


def font_file() -> str | None:
    if not shutil.which("fc-match"):
        return None
    out = subprocess.run(["fc-match", "-f", "%{file}", "sans:bold"], capture_output=True, text=True).stdout.strip()
    return out or None


def _card_filter(n: int, label: str, weak: bool, font: str | None) -> str:
    chain = "color=c=0x1b2a6b,drawbox=x=iw*0.05:y=ih*0.08:w=iw*0.30:h=ih*0.25:color=0xffcc33:t=fill"
    chain += ",drawbox=x=iw*0.10:y=ih*0.13:w=iw*0.12:h=ih*0.15:color=0x1b2a6b:t=fill"  # a hole, so the box has shape
    if font:
        chain += f",drawtext=fontfile='{font}':text='{label}':fontsize=40:fontcolor=white:x=(w-tw)/2:y=h*0.62"
    if weak:
        chain += ",noise=alls=100:allf=t,eq=brightness=-0.2:contrast=0.7"
    return chain


def make_compilation(
    out: Path, episode_s: list[float], *, card_s: float = 2.0, black_s: float = 0.6, weak: set[int] = frozenset(),
    lead_s: float = 3.0,
) -> Compilation:
    """A compilation of len(episode_s) episodes; ``weak`` holds 0-based indices of weakened cards.

    It starts with ``lead_s`` of content (a channel intro) before the first black and card.
    """
    font = font_file()
    inputs: list[str] = []
    labels: list[str] = []
    t = 0.0
    card_starts, black_starts, titles = [], [], []

    def add(source: str, seconds: float) -> None:
        """A lavfi source (or a chain starting with one) limited to ``seconds`` by -t."""
        head, sep, chain = source.partition(",")
        head += (":" if "=" in head else "=") + f"s={SIZE}:r={RATE}"
        inputs.extend(["-t", f"{seconds}", "-f", "lavfi", "-i", head + sep + chain])
        labels.append(f"[{len(labels)}:v]")

    if lead_s:
        add("smptehdbars", lead_s)
        t += lead_s
    for i, length in enumerate(episode_s):
        add("color=c=black", black_s)
        black_starts.append(round(t, 3))
        t += black_s
        title = f"Episode {i + 1}"
        titles.append(title)
        add(_card_filter(i, title, i in weak, font), card_s)
        card_starts.append(round(t, 3))
        t += card_s
        add(CONTENT[i % len(CONTENT)], length)
        t += length
    n = len(labels)
    graph = "".join(f"{lbl}setsar=1,format=yuv420p,fps={RATE}[s{k}];" for k, lbl in enumerate(labels))
    graph += "".join(f"[s{k}]" for k in range(n)) + f"concat=n={n}:v=1:a=0[v]"
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y", *inputs,
         "-f", "lavfi", "-i", f"sine=frequency=440:duration={t}",
         "-filter_complex", graph, "-map", "[v]", "-map", f"{n}:a",
         "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest",
         "-movflags", "+faststart", str(out)],
        check=True,
    )
    reference = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-ss", f"{card_starts[0] + card_s / 2}",
         "-i", str(out), "-frames:v", "1", "-f", "image2", "-vcodec", "mjpeg", "pipe:1"],
        check=True, capture_output=True,
    ).stdout
    return Compilation(out, round(t, 3), card_starts, black_starts, reference, titles)
