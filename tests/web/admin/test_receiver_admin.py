"""Tellybox receiver (CR-1, CR-6) in the admin: the app ID setting and the dashboard's TV receiver line."""

from __future__ import annotations

import re

import pytest

from tests.web.admin.test_settings import VALID_FORM


def _app_id(admin_env):
    return admin_env.conn.execute("SELECT receiver_app_id FROM settings WHERE id = 1").fetchone()[0]


# --------------------------------------------------------------------------- settings


def test_settings_shows_receiver_app_id_field(admin, admin_env):
    r = admin.get("/admin/settings")
    assert 'name="receiver_app_id"' in r.text
    assert "Tellybox receiver app ID" in r.text
    assert "installation.md" in r.text


def test_receiver_app_id_saved_uppercased(admin, admin_env):
    r = admin.post("/admin/settings", data={**VALID_FORM, "receiver_app_id": " 1a2b3c4d "}, follow_redirects=False)
    assert r.status_code == 303
    assert _app_id(admin_env) == "1A2B3C4D"
    assert 'value="1A2B3C4D"' in admin.get("/admin/settings").text


def test_receiver_app_id_empty_is_null(admin, admin_env):
    admin.post("/admin/settings", data={**VALID_FORM, "receiver_app_id": "1A2B3C4D"}, follow_redirects=False)
    admin.post("/admin/settings", data={**VALID_FORM, "receiver_app_id": "  "}, follow_redirects=False)
    assert _app_id(admin_env) is None


def test_receiver_app_id_missing_field_is_null(admin, admin_env):
    admin.post("/admin/settings", data=VALID_FORM, follow_redirects=False)
    assert _app_id(admin_env) is None


@pytest.mark.parametrize("bad", ["1A2B3C4", "1A2B3C4DE", "GHIJKLMN", "1A2B-C4D"])
def test_receiver_app_id_rejects_bad_values(admin, admin_env, bad):
    r = admin.post("/admin/settings", data={**VALID_FORM, "receiver_app_id": bad}, follow_redirects=False)
    assert r.status_code == 422
    assert _app_id(admin_env) is None
    assert f'value="{bad}"' in r.text


# --------------------------------------------------------------------------- dashboard line


def _line(response) -> str:
    """The text of the TV receiver line (the page also embeds every JS string in a JSON catalog)."""
    return re.search(r'<span id="receiver">(.*?)</span>', response.text, re.S).group(1)


def test_dashboard_receiver_line_default(admin, admin_env):
    r = admin.get("/admin")
    assert 'id="receiver"' in r.text
    assert _line(r) == "Default Media Receiver"


def test_dashboard_receiver_line_tellybox(admin, admin_env, mkstate):
    admin_env.cast.current = mkstate(receiver={"kind": "tellybox", "configured": True,
                                               "fallback_until": None, "last_error": None})
    r = admin.get("/admin")
    assert _line(r) == "Tellybox receiver"


def test_dashboard_receiver_line_fallback_is_escaped_and_local(admin, admin_env, mkstate):
    # NOW is 14:00 UTC = 16:00 in Amsterdam; the fallback ends 30 min later.
    admin_env.cast.current = mkstate(receiver={
        "kind": "default", "configured": True, "fallback_until": "2026-09-28T14:30:00+00:00",
        "last_error": "<b>launch timed out</b>"})
    r = admin.get("/admin")
    assert _line(r) == ("Default Media Receiver (Tellybox receiver unavailable until 16:30: "
                        "&lt;b&gt;launch timed out&lt;/b&gt;)")


def test_dashboard_receiver_line_ignores_an_expired_fallback(admin, admin_env, mkstate):
    admin_env.cast.current = mkstate(receiver={
        "kind": "default", "configured": True, "fallback_until": "2026-09-28T13:00:00+00:00", "last_error": "old"})
    r = admin.get("/admin")
    assert _line(r) == "Default Media Receiver"


def test_dashboard_receiver_line_hidden_without_receiver_block(admin, admin_env, mkstate):
    state = mkstate()
    del state["receiver"]
    admin_env.cast.current = state
    r = admin.get("/admin")
    assert r.status_code == 200
    assert 'id="receiver-row" hidden' in r.text


def test_dashboard_receiver_line_is_translated(admin, admin_env):
    r = admin.get("/admin", headers={"Accept-Language": "nl"})
    assert _line(r) == "Standaard mediaontvanger"
