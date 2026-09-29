"""The Tellybox receiver's static files are served at /receiver/ (CR-1), never cached."""

from __future__ import annotations

from fastapi.testclient import TestClient

from tellybox import db
from tellybox.web import app as app_module
from tellybox.web.app import create_app


def test_receiver_files_are_served_uncached(config, fake_cast, tmp_path, monkeypatch):
    receiver = tmp_path / "receiver"
    receiver.mkdir()
    (receiver / "index.html").write_text("<!doctype html>receiver")
    monkeypatch.setattr(app_module, "RECEIVER_DIR", receiver)
    client = TestClient(create_app(config, conn=db.open_db(config.db_path), cast=fake_cast))
    r = client.get("/receiver/index.html")
    assert r.status_code == 200 and "receiver" in r.text
    assert r.headers["cache-control"] == "no-cache"
    assert client.get("/receiver/nope.js").status_code == 404


def test_real_receiver_directory_is_mounted(config, fake_cast):
    client = TestClient(create_app(config, conn=db.open_db(config.db_path), cast=fake_cast))
    r = client.get("/receiver/index.html")
    assert r.status_code == 200
    assert r.headers["cache-control"] == "no-cache"
