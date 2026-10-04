"""A short browser label from the User-Agent, such as "iPhone Safari" (PB-7, WT-12, AD-4).

Derived on the server and never taken from the client; shown to the admin only.
"""

from __future__ import annotations

LABEL_MAX = 40
FALLBACK = "Browser"

# Order matters: iPadOS and iOS UAs mention Mac and Safari too, Android UAs mention Linux, and
# Edge/Opera/Samsung/iOS Chrome all mention Chrome or Safari.
_SYSTEMS = (("iPhone", "iPhone"), ("iPad", "iPad"), ("Android", "Android"), ("CrOS", "ChromeOS"),
            ("Windows", "Windows"), ("Macintosh", "Mac"), ("Linux", "Linux"), ("X11", "Linux"))
_BROWSERS = (("Edg/", "Edge"), ("EdgA/", "Edge"), ("EdgiOS/", "Edge"), ("OPR/", "Opera"),
             ("SamsungBrowser/", "Samsung Internet"), ("FxiOS/", "Firefox"), ("Firefox/", "Firefox"),
             ("CriOS/", "Chrome"), ("Chrome/", "Chrome"), ("Safari/", "Safari"))


def browser_label(user_agent: str | None) -> str:
    """"<system> <browser>" for a User-Agent, at most 40 characters; "Browser" when nothing is recognised."""
    ua = user_agent or ""
    system = next((name for key, name in _SYSTEMS if key in ua), None)
    browser = next((name for key, name in _BROWSERS if key in ua), None)
    label = " ".join(p for p in (system, browser) if p) or FALLBACK
    return label[:LABEL_MAX]
