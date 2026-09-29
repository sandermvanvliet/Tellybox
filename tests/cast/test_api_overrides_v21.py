"""POST /overrides in v2.1 (HA-3, HA-7): profile_ids, clear, source, validation."""

from test_api import env  # noqa: F401  (fixture)


async def _post(env, **body):
    return await env["client"].post("/overrides", json=body)


async def test_override_profile_ids_and_source(env):  # noqa: F811
    r = await _post(env, kind="unlimited", profile_ids=[1], source="Home Assistant")
    assert r.status_code == 200
    assert r.json()["timer"]["profiles"][0]["unlimited"] is True
    assert env["conn"].execute("SELECT source FROM override_log").fetchone()[0] == "Home Assistant"


async def test_override_clear(env):  # noqa: F811
    await _post(env, kind="block")
    r = await _post(env, kind="clear")
    assert r.status_code == 200
    assert r.json()["timer"]["profiles"][0]["blocked"] is False


async def test_override_profile_id_still_works(env):  # noqa: F811
    assert (await _post(env, kind="block", profile_id=1)).status_code == 200
    assert env["ctrl"].state()["timer"]["profiles"][0]["blocked"] is True


async def test_override_validation(env):  # noqa: F811
    assert (await _post(env, kind="block", profile_ids=[99])).status_code == 422
    assert (await _post(env, kind="block", profile_id=99)).status_code == 422
    assert (await _post(env, kind="block", profile_id=1, profile_ids=[1])).status_code == 422
    assert (await _post(env, kind="block", profile_ids=[])).status_code == 422
    assert (await _post(env, kind="block", profile_ids=list(range(1, 22)))).status_code == 422
    assert (await _post(env, kind="block", source="x" * 65)).status_code == 422
    assert (await _post(env, kind="block", source="x" * 64)).status_code == 200


async def test_state_has_v21_fields(env):  # noqa: F811
    timer = (await env["client"].get("/state")).json()["timer"]
    assert "next_reset" in timer
    assert timer["profiles"][0]["session_elapsed_s"] is None
