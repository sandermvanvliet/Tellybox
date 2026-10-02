"""Settings `/admin/settings` (AD-2, PB-1): allowance/mode/max session, reset/grace/break,
and the Chromecast picker.
"""

from __future__ import annotations

import pytest

from tests.web.conftest import PROFILE

VALID_FORM = {
    f"profile_{PROFILE}_allowance_min": "45",
    f"profile_{PROFILE}_counting_mode": "wall_clock",
    f"profile_{PROFILE}_max_session_min": "70",
    "reset_time": "05:30",
    "grace_cap_min": "10",
    "session_break_min": "20",
}


def test_settings_renders_defaults(admin, admin_env):
    r = admin.get("/admin/settings")
    assert r.status_code == 200
    assert 'value="60"' in r.text  # default daily allowance
    assert 'value="04:00"' in r.text


def test_settings_renders_with_cast_down(admin, admin_env):
    admin_env.cast.mode = "down"
    r = admin.get("/admin/settings")
    assert r.status_code == 200


def test_save_settings_writes_db_and_flashes(admin, admin_env):
    r = admin.post("/admin/settings", data=VALID_FORM, follow_redirects=False)
    assert r.status_code == 303
    follow = admin.get(r.headers["location"])
    assert "picks this up within 15" in follow.text

    row = admin_env.conn.execute(
        "SELECT daily_allowance_min, counting_mode, max_session_min FROM profile WHERE id = ?", (PROFILE,)
    ).fetchone()
    assert (row["daily_allowance_min"], row["counting_mode"], row["max_session_min"]) == (45, "wall_clock", 70)

    settings_row = admin_env.conn.execute(
        "SELECT reset_time, grace_cap_min, session_break_min FROM settings WHERE id = 1"
    ).fetchone()
    assert (settings_row["reset_time"], settings_row["grace_cap_min"], settings_row["session_break_min"]) == (
        "05:30", 10, 20,
    )


@pytest.mark.parametrize("field,bad_value", [
    (f"profile_{PROFILE}_allowance_min", "0"),
    (f"profile_{PROFILE}_allowance_min", "-5"),
    (f"profile_{PROFILE}_allowance_min", "abc"),
    (f"profile_{PROFILE}_max_session_min", "0"),
    ("reset_time", "25:00"),
    ("reset_time", "4pm"),
    ("grace_cap_min", "-1"),
    ("grace_cap_min", "61"),
    ("session_break_min", "0"),
])
def test_save_settings_rejects_bad_input_and_writes_nothing(admin, admin_env, field, bad_value):
    before_profile = dict(admin_env.conn.execute(
        "SELECT daily_allowance_min, counting_mode, max_session_min FROM profile WHERE id = ?", (PROFILE,)
    ).fetchone())
    before_settings = dict(admin_env.conn.execute(
        "SELECT reset_time, grace_cap_min, session_break_min FROM settings WHERE id = 1"
    ).fetchone())

    form = {**VALID_FORM, field: bad_value}
    r = admin.post("/admin/settings", data=form, follow_redirects=False)
    assert r.status_code == 422

    after_profile = dict(admin_env.conn.execute(
        "SELECT daily_allowance_min, counting_mode, max_session_min FROM profile WHERE id = ?", (PROFILE,)
    ).fetchone())
    after_settings = dict(admin_env.conn.execute(
        "SELECT reset_time, grace_cap_min, session_break_min FROM settings WHERE id = 1"
    ).fetchone())
    assert after_profile == before_profile
    assert after_settings == before_settings


def test_save_settings_rejects_unknown_counting_mode(admin, admin_env):
    form = {**VALID_FORM, f"profile_{PROFILE}_counting_mode": "sometimes"}
    r = admin.post("/admin/settings", data=form, follow_redirects=False)
    assert r.status_code == 422


def test_save_settings_re_renders_entered_values_on_error(admin, admin_env):
    form = {**VALID_FORM, f"profile_{PROFILE}_allowance_min": "0"}
    r = admin.post("/admin/settings", data=form, follow_redirects=False)
    assert r.status_code == 422
    assert 'value="0"' in r.text  # the invalid field, as entered
    assert 'value="05:30"' in r.text  # other entered values are preserved too


def test_device_search_goes_through_fake_cast(admin, admin_env):
    admin_env.cast.devices_result = {"selected": None, "devices": [{"uuid": "abc", "name": "Living Room TV"}]}
    r = admin.post("/admin/settings/devices/search", follow_redirects=False)
    assert r.status_code == 303
    assert ("devices",) in admin_env.cast.calls
    follow = admin.get(r.headers["location"])
    assert "Living Room TV" in follow.text


def test_device_search_unreachable_flashes_not_500(admin, admin_env):
    admin_env.cast.mode = "down"
    r = admin.post("/admin/settings/devices/search", follow_redirects=False)
    assert r.status_code == 303
    follow = admin.get(r.headers["location"])
    assert "unreachable" in follow.text.lower()


def test_device_select_goes_through_fake_cast(admin, admin_env):
    r = admin.post("/admin/settings/devices/select", data={"uuid": "abc"}, follow_redirects=False)
    assert r.status_code == 303
    assert ("select_device", "abc") in admin_env.cast.calls


def test_device_select_not_found(admin, admin_env):
    admin_env.cast.mode = "not_found"
    r = admin.post("/admin/settings/devices/select", data={"uuid": "nope"}, follow_redirects=False)
    assert r.status_code == 303
    follow = admin.get(r.headers["location"])
    assert "unknown device" in follow.text.lower()


def test_known_devices_marks_the_selected_one(admin, admin_env):
    admin_env.conn.execute(
        "INSERT INTO cast_device (uuid, name, host, port, selected, last_seen_at) VALUES (?, ?, ?, ?, ?, ?)",
        ("abc", "Living Room TV", "192.168.1.137", 8009, 1, "2026-09-28T12:00:00.000Z"),
    )
    r = admin.get("/admin/settings")
    assert r.status_code == 200
    assert "Living Room TV" in r.text
    assert "selected" in r.text.lower()


def test_settings_shows_each_profiles_avatar_in_order(admin, admin_env):
    conn = admin_env.conn
    conn.execute("UPDATE profile SET name = 'Mila', avatar = 'fox' WHERE id = 1")
    conn.execute("INSERT INTO profile (id, name, avatar, sort_order, created_at) VALUES (2, 'Noor', 'owl', 0, 'x')")
    text = admin.get("/admin/settings").text
    assert "/static/avatars/fox.svg" in text and "/static/avatars/owl.svg" in text
    assert text.index("Noor") < text.index("Mila")


# --------------------------------------------------------------------------- reader UI and TV per profile (KA-11, PB-6)


def _remember_tvs(conn):
    conn.executemany(
        "INSERT INTO cast_device (uuid, name, host, port, model, last_seen_at) VALUES (?, ?, '192.0.2.1', 8009, 'Chromecast', 'x')",
        [("uuid-living", "Living Room TV"), ("uuid-bedroom", "Bedroom TV")],
    )


def test_settings_defaults_for_ui_mode_and_tv(admin, admin_env):
    _remember_tvs(admin_env.conn)
    r = admin.get("/admin/settings")
    assert f'name="profile_{PROFILE}_ui_mode"' in r.text
    assert f'name="profile_{PROFILE}_cast_device"' in r.text
    assert "Bedroom TV" in r.text


def test_save_ui_mode_and_tv(admin, admin_env):
    _remember_tvs(admin_env.conn)
    form = {**VALID_FORM, f"profile_{PROFILE}_ui_mode": "text", f"profile_{PROFILE}_cast_device": "uuid-bedroom"}
    assert admin.post("/admin/settings", data=form, follow_redirects=False).status_code == 303
    row = admin_env.conn.execute("SELECT ui_mode, cast_device_uuid FROM profile WHERE id = ?", (PROFILE,)).fetchone()
    assert (row["ui_mode"], row["cast_device_uuid"]) == ("text", "uuid-bedroom")
    # Back to the defaults.
    form = {**VALID_FORM, f"profile_{PROFILE}_ui_mode": "icons", f"profile_{PROFILE}_cast_device": ""}
    assert admin.post("/admin/settings", data=form, follow_redirects=False).status_code == 303
    row = admin_env.conn.execute("SELECT ui_mode, cast_device_uuid FROM profile WHERE id = ?", (PROFILE,)).fetchone()
    assert (row["ui_mode"], row["cast_device_uuid"]) == ("icons", None)


def test_form_without_the_new_fields_leaves_them_alone(admin, admin_env):
    _remember_tvs(admin_env.conn)
    admin_env.conn.execute("UPDATE profile SET ui_mode = 'text', cast_device_uuid = 'uuid-living'")
    assert admin.post("/admin/settings", data=VALID_FORM, follow_redirects=False).status_code == 303
    row = admin_env.conn.execute("SELECT ui_mode, cast_device_uuid FROM profile WHERE id = ?", (PROFILE,)).fetchone()
    assert (row["ui_mode"], row["cast_device_uuid"]) == ("text", "uuid-living")


@pytest.mark.parametrize("field,bad_value", [("ui_mode", "fancy"), ("ui_mode", ""), ("cast_device", "no-such-tv")])
def test_bad_ui_mode_or_tv_is_rejected_and_nothing_written(admin, admin_env, field, bad_value):
    _remember_tvs(admin_env.conn)
    form = {**VALID_FORM, f"profile_{PROFILE}_{field}": bad_value}
    assert admin.post("/admin/settings", data=form).status_code == 422
    row = admin_env.conn.execute(
        "SELECT daily_allowance_min, ui_mode, cast_device_uuid FROM profile WHERE id = ?", (PROFILE,)
    ).fetchone()
    assert (row["daily_allowance_min"], row["ui_mode"], row["cast_device_uuid"]) == (60, "icons", None)
