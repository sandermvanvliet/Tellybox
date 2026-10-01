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
    N_("Last problem: %(problem)s at %(time)s"),
    N_("The Tellybox receiver did not start"),
    N_("The Chromecast refused the Tellybox receiver"),
    N_("The Tellybox receiver disappeared during an episode"),
    N_("The Tellybox receiver could not be restarted during an episode"),
    N_("The Tellybox receiver page reported a problem"),
    N_("Check SponsorBlock"),
    N_("Download again"),
    N_("Split into episodes"),
    N_("Find title cards"),
    # dashboard.js and jobs.js: job statuses
    N_("queued"),
    N_("downloading"),
    N_("processing"),
    N_("ready"),
    # split.js: the split editor
    N_("Part %(n)d"),
    N_("Keep"),
    N_("Left out"),
    N_("Cut at %(time)s"),
    N_("Go to"),
    N_("Go to part %(n)d at %(time)s"),
    N_("Remove cut"),
    N_("Title"),
    N_("Title (optional)"),
    N_("Move the cut back by %(amount)s s"),
    N_("Move the cut forward by %(amount)s s"),
    N_("Saving…"),
    N_("Saved"),
    N_("Saving failed. Your changes are not saved yet."),
    N_("This plan can't be changed any more."),
    N_("A cut needs at least half a second on both sides."),
    N_("Replace your cuts with the video's chapters?"),
    N_("The plan has no parts."),
    N_("A video can be split into at most %(n)d parts."),
    N_("The first part must start at the beginning of the video."),
    N_("The last part must end at the end of the video."),
    N_("Parts must follow each other without gaps."),
    N_("Titles can be at most %(n)d characters."),
    N_("Each part must be at least %(n)d seconds long."),
    N_("Keep at least one part."),
    N_("Add a cut, or leave out a part, before approving."),
    # split.js: title cards and detected cuts (v6)
    N_("sure"),
    N_("check"),
    N_("unsure"),
    N_("black"),
    N_("scene change"),
    N_("at the title card"),
    N_("Saving the title card…"),
    N_("Saving the title card failed."),
    N_("Title card saved."),
    N_("Replace your cuts with the detected ones?"),
    N_("Finding cuts…"),
    N_("Finding cuts failed."),
    N_("No title cards found in this video."),
    N_("Finding cuts finished without a new result. The Jobs page may say why."),
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
    Nn_("Cutting takes up to about %(num)d minute", "Cutting takes up to about %(num)d minutes"),  # split.js
    Nn_("Found %(num)d cut. Check each one before approving.", "Found %(num)d cuts. Check each one before approving."),  # split.js
]


def js_catalog() -> dict:
    """{"s": {msgid: text}, "p": {singular: [one, other]}} in the current request's language."""
    return {
        "s": {m: gettext(m) for m in STRINGS},
        "p": {s: [ngettext(s, p, 1), ngettext(s, p, 2)] for s, p in PLURALS},
    }
