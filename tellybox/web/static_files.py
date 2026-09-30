"""Static file mounts that browsers always revalidate."""

from __future__ import annotations

from fastapi.staticfiles import StaticFiles


class NoCacheStaticFiles(StaticFiles):
    """`Cache-Control: no-cache`: browsers keep the file but ask each time (a 304 via ETag when unchanged).

    Without the header, browsers cache heuristically for a share of the file's age, so scripts and styles
    stayed stale after a deploy until a forced refresh.
    """

    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache"
        return response
