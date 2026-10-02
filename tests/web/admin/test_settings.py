"""Settings `/admin/settings` (AD-2, PB-1): allowance/mode/max session, reset/grace/break,
and the Chromecast picker.
"""

from __future__ import annotations

import pytest

from tests.web.conftest import PROFILE

VALID_FORM = {
    f"profile_{PROFILE}_allowance_mode": "custom",
    f"profile_{PROFILE}_allowance_min": "45",
    f"profile_{PROFILE}_counting_mode": "wall_clock",
    f"profile_{PROFILE}_max_session_mode": "custom",
    f"profile_{PROFILE}_max_session_min": "70",
    "default_allowance_min": "60",
    "default_max_session_min": "90",
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


def test_allowance_mode_inherit_roundtrips(admin, admin_env):
    form = {**VALID_FORM, f"profile_{PROFILE}_allowance_mode": "inherit"}
    r = admin.post("/admin/settings", data=form, follow_redirects=False)
    assert r.status_code == 303

    row = admin_env.conn.execute(
        "SELECT allowance_mode, daily_allowance_min FROM profile WHERE id = ?", (PROFILE,)
    ).fetchone()
    assert row["allowance_mode"] == "inherit"


def test_allowance_mode_custom_roundtrips(admin, admin_env):
    form = {**VALID_FORM, f"profile_{PROFILE}_allowance_mode": "custom"}
    r = admin.post("/admin/settings", data=form, follow_redirects=False)
    assert r.status_code == 303

    row = admin_env.conn.execute(
        "SELECT allowance_mode, daily_allowance_min FROM profile WHERE id = ?", (PROFILE,)
    ).fetchone()
    assert row["allowance_mode"] == "custom"
    assert row["daily_allowance_min"] == 45


def test_allowance_mode_unlimited_roundtrips(admin, admin_env):
    form = {**VALID_FORM, f"profile_{PROFILE}_allowance_mode": "unlimited"}
    r = admin.post("/admin/settings", data=form, follow_redirects=False)
    assert r.status_code == 303

    row = admin_env.conn.execute(
        "SELECT allowance_mode, daily_allowance_min FROM profile WHERE id = ?", (PROFILE,)
    ).fetchone()
    assert row["allowance_mode"] == "unlimited"


def test_max_session_mode_roundtrips(admin, admin_env):
    for mode in ["inherit", "custom", "unlimited"]:
        form = {**VALID_FORM, f"profile_{PROFILE}_max_session_mode": mode}
        r = admin.post("/admin/settings", data=form, follow_redirects=False)
        assert r.status_code == 303

        row = admin_env.conn.execute(
            "SELECT max_session_mode FROM profile WHERE id = ?", (PROFILE,)
        ).fetchone()
        assert row["max_session_mode"] == mode


def test_invalid_allowance_mode_rejected(admin, admin_env):
    form = {**VALID_FORM, f"profile_{PROFILE}_allowance_mode": "maybe"}
    r = admin.post("/admin/settings", data=form, follow_redirects=False)
    assert r.status_code == 422


def test_invalid_max_session_mode_rejected(admin, admin_env):
    form = {**VALID_FORM, f"profile_{PROFILE}_max_session_mode": "sometimes"}
    r = admin.post("/admin/settings", data=form, follow_redirects=False)
    assert r.status_code == 422


def test_custom_allowance_validates_number(admin, admin_env):
    form = {**VALID_FORM, f"profile_{PROFILE}_allowance_mode": "custom", f"profile_{PROFILE}_allowance_min": "0"}
    r = admin.post("/admin/settings", data=form, follow_redirects=False)
    assert r.status_code == 422
    assert "error" in r.text.lower()


def test_custom_allowance_not_validated_when_inherit(admin, admin_env):
    form = {**VALID_FORM, f"profile_{PROFILE}_allowance_mode": "inherit", f"profile_{PROFILE}_allowance_min": "0"}
    r = admin.post("/admin/settings", data=form, follow_redirects=False)
    assert r.status_code == 303


def test_custom_max_session_validates_number(admin, admin_env):
    form = {**VALID_FORM, f"profile_{PROFILE}_max_session_mode": "custom", f"profile_{PROFILE}_max_session_min": "0"}
    r = admin.post("/admin/settings", data=form, follow_redirects=False)
    assert r.status_code == 422


def test_custom_max_session_not_validated_when_unlimited(admin, admin_env):
    form = {**VALID_FORM, f"profile_{PROFILE}_max_session_mode": "unlimited", f"profile_{PROFILE}_max_session_min": "0"}
    r = admin.post("/admin/settings", data=form, follow_redirects=False)
    assert r.status_code == 303


def test_default_allowance_saved(admin, admin_env):
    form = {**VALID_FORM, "default_allowance_min": "75"}
    r = admin.post("/admin/settings", data=form, follow_redirects=False)
    assert r.status_code == 303

    row = admin_env.conn.execute(
        "SELECT default_allowance_min FROM settings WHERE id = 1"
    ).fetchone()
    assert row["default_allowance_min"] == 75


def test_default_max_session_saved(admin, admin_env):
    form = {**VALID_FORM, "default_max_session_min": "120"}
    r = admin.post("/admin/settings", data=form, follow_redirects=False)
    assert r.status_code == 303

    row = admin_env.conn.execute(
        "SELECT default_max_session_min FROM settings WHERE id = 1"
    ).fetchone()
    assert row["default_max_session_min"] == 120


def test_invalid_default_allowance_rejected(admin, admin_env):
    form = {**VALID_FORM, "default_allowance_min": "0"}
    r = admin.post("/admin/settings", data=form, follow_redirects=False)
    assert r.status_code == 422


def test_invalid_default_max_session_rejected(admin, admin_env):
    form = {**VALID_FORM, "default_max_session_min": "-5"}
    r = admin.post("/admin/settings", data=form, follow_redirects=False)
    assert r.status_code == 422


def test_settings_page_renders_modes_selected(admin, admin_env):
    admin_env.conn.execute(
        "UPDATE profile SET allowance_mode = 'unlimited', max_session_mode = 'custom' WHERE id = ?", (PROFILE,)
    )
    r = admin.get("/admin/settings")
    assert r.status_code == 200
    # Check that unlimited and custom are selected
    assert f'<option value="unlimited" selected>' in r.text or 'value="unlimited" selected' in r.text


def test_custom_value_survives_switching_to_default_and_back(admin, admin_env):  # A-23
    """Saving a profile as inherit/unlimited must not overwrite the stored custom minutes."""
    admin.post("/admin/settings", data={**VALID_FORM, f"profile_{PROFILE}_allowance_mode": "custom",
                                        f"profile_{PROFILE}_allowance_min": "45",
                                        f"profile_{PROFILE}_max_session_mode": "custom",
                                        f"profile_{PROFILE}_max_session_min": "75"}, follow_redirects=False)
    admin.post("/admin/settings", data={**VALID_FORM, f"profile_{PROFILE}_allowance_mode": "unlimited",
                                        f"profile_{PROFILE}_allowance_min": "",
                                        f"profile_{PROFILE}_max_session_mode": "inherit",
                                        f"profile_{PROFILE}_max_session_min": ""}, follow_redirects=False)
    row = admin_env.conn.execute(
        "SELECT daily_allowance_min, max_session_min FROM profile WHERE id = ?", (PROFILE,)).fetchone()
    assert (row["daily_allowance_min"], row["max_session_min"]) == (45, 75)
