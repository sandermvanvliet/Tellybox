"""Shared test setup.

Show access (PR-5): in production a new show or profile starts with nothing visible. Most tests
are about something else and create shows and profiles freely, so by default every test database
gets triggers that grant each new show to every profile and each new profile every show. Tests of
show access itself opt out with `@pytest.mark.strict_access`.
"""

from __future__ import annotations

import pytest

from tellybox import db

_GRANT_TRIGGERS = (
    """CREATE TRIGGER IF NOT EXISTS test_grant_new_show AFTER INSERT ON show BEGIN
         INSERT OR IGNORE INTO profile_show (profile_id, show_id) SELECT id, NEW.id FROM profile; END""",
    """CREATE TRIGGER IF NOT EXISTS test_grant_new_profile AFTER INSERT ON profile BEGIN
         INSERT OR IGNORE INTO profile_show (profile_id, show_id) SELECT NEW.id, id FROM show; END""",
)


@pytest.fixture(autouse=True)
def _open_show_access(request, monkeypatch):
    if request.node.get_closest_marker("strict_access"):
        return
    real_migrate = db.migrate

    def migrate(conn):
        version = real_migrate(conn)
        for sql in _GRANT_TRIGGERS:
            conn.execute(sql)
        return version

    monkeypatch.setattr(db, "migrate", migrate)
