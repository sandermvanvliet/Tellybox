"""Text the admin JavaScript writes at runtime (NF-13).

Listed here so extraction puts them in the catalogs; base.html embeds their translations for
the current request as JSON (`js_catalog`), and admin/static/i18n.js looks them up:
t("Nothing is playing.") and tn("%(num)d failed job", "%(num)d failed jobs", n).
Placeholders use Python's %(name)s syntax in both places.
"""

from __future__ import annotations

from tellybox.i18n import N_, Nn_, gettext, ngettext

# Single strings: t(msgid, vars)
STRINGS: list[str] = [
    # dashboard.js: the Chromecast connection, the player and the jobs
    N_("connecting"),
    N_("connected"),
    N_("lost"),
    N_("failed"),
    N_("disconnected"),
    N_("unreachable"),
    N_("loading"),
    N_("playing"),
    N_("buffering"),
    N_("paused"),
    N_("unknown"),
    N_("Nothing is playing."),
    N_("Stop"),
    N_("Watching: %(names)s"),
    N_("%(time)s used"),
    N_("%(time)s left"),
    N_("Media on disk: %(size)s"),
    N_("Free space: %(size)s"),
    N_("Nothing downloading."),
    N_("download"),
    N_("Update yt-dlp"),
    N_("Tellybox receiver"),
    N_("Default Media Receiver"),
    N_("Default Media Receiver (Tellybox receiver unavailable until %(time)s: %(reason)s)"),
    N_("Check SponsorBlock"),
    N_("Download again"),
    # dashboard.js and jobs.js: job statuses
    N_("queued"),
    N_("downloading"),
    N_("processing"),
    N_("ready"),
    # formatting, the twins of the minutes and bytes filters (common.py)
    N_("%(hours)s h %(minutes)s min"),
    N_("%(minutes)s min"),
    N_("%(size)s B"),
    N_("KB"),
    N_("MB"),
    N_("GB"),
    N_("TB"),
]

# Plurals: tn(singular, plural, n, vars); `num` is always available as a placeholder.
PLURALS: list[tuple[str, str]] = [
    Nn_("Add %(num)d video", "Add %(num)d videos"),  # add.js
    Nn_("%(num)d failed job", "%(num)d failed jobs"),  # dashboard.js
]


def js_catalog() -> dict:
    """{"s": {msgid: text}, "p": {singular: [one, other]}} in the current request's language."""
    return {
        "s": {m: gettext(m) for m in STRINGS},
        "p": {s: [ngettext(s, p, 1), ngettext(s, p, 2)] for s, p in PLURALS},
    }
