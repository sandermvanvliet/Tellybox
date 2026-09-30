"""Scripts and styles are revalidated on every load, so a deploy shows without a forced refresh."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tellybox import db
from tellybox.web.app import create_app


@pytest.mark.parametrize("path", ["/static/app.js", "/static/app.css", "/admin/static/admin.css", "/admin/static/dashboard.js"])
def test_static_files_are_revalidated(config, fake_cast, path):
    client = TestClient(create_app(config, conn=db.open_db(config.db_path), cast=fake_cast))
    r = client.get(path)
    assert r.status_code == 200
    assert r.headers["cache-control"] == "no-cache"
    again = client.get(path, headers={"If-None-Match": r.headers["etag"]})
    assert again.status_code == 304  # unchanged files cost a tiny round trip
