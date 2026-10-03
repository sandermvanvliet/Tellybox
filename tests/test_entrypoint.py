import os
import subprocess

import pytest

from tellybox import entrypoint, privileges


def test_data_dirs_from_env():
    dirs = entrypoint.data_dirs({"TELLYBOX_DATA_DIR": "/x", "TELLYBOX_MEDIA_DIR": "/y"})
    assert [str(d) for d in dirs] == ["/x", "/y", "/backups"]


@pytest.fixture
def spy(monkeypatch):
    rec = {"exec": None, "drop": None, "chown": []}
    monkeypatch.setattr(os, "execvp", lambda f, a: rec.__setitem__("exec", (f, a)))
    monkeypatch.setattr(privileges, "drop_root", lambda u, g: rec.__setitem__("drop", (u, g)))
    monkeypatch.setattr(subprocess, "run", lambda cmd, check: rec["chown"].append(cmd))
    return rec


def test_non_root_execs_unchanged(monkeypatch, spy):
    monkeypatch.setattr(os, "geteuid", lambda: 1500)
    entrypoint.main(["tellybox", "migrate"])
    assert spy["exec"] == ("tellybox", ["tellybox", "migrate"])
    assert spy["drop"] is None and spy["chown"] == []


def test_root_chowns_drops_and_execs(monkeypatch, tmp_path, spy):
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setenv("TELLYBOX_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("TELLYBOX_MEDIA_DIR", str(tmp_path / "nope"))
    monkeypatch.setenv("PUID", str(os.getuid() + 1))
    monkeypatch.setenv("PGID", str(os.getgid()))
    entrypoint.main([])
    owner = f"{os.getuid() + 1}:{os.getgid()}"
    # the missing media dir is created first, then chowned too
    assert spy["chown"] == [
        ["chown", "-R", owner, str(tmp_path)],
        ["chown", "-R", owner, str(tmp_path / "nope")],
    ]
    assert (tmp_path / "nope").is_dir()
    assert spy["drop"] == (os.getuid() + 1, os.getgid())
    assert spy["exec"] == ("python", entrypoint.DEFAULT_COMMAND)


def test_root_skips_chown_when_owner_matches(monkeypatch, tmp_path, spy):
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setenv("TELLYBOX_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("TELLYBOX_MEDIA_DIR", str(tmp_path / "nope"))
    monkeypatch.setenv("PUID", str(os.getuid()))
    monkeypatch.setenv("PGID", str(os.getgid()))
    entrypoint.main(["x"])
    assert spy["chown"] == []


def test_bad_puid_exits(monkeypatch, spy):
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setenv("PUID", "nope")
    with pytest.raises(SystemExit):
        entrypoint.main(["x"])
    assert spy["exec"] is None


def test_options_to_env_maps_and_skips_empty():
    opts = {"web_port": 9000, "cast_api_port": 9001, "media_base_url": "", "admin_password": "pw"}
    assert entrypoint.options_to_env(opts, {}) == {
        "TELLYBOX_WEB_PORT": "9000",
        "TELLYBOX_CAST_API_PORT": "9001",
        "TELLYBOX_ADMIN_PASSWORD": "pw",
    }


def test_options_to_env_maps_the_oidc_options():  # AD-6
    opts = {"oidc_issuer": "https://id.example.org", "oidc_client_id": "tellybox", "oidc_client_secret": "s",
            "oidc_redirect_uri": "https://tellybox.example.org/admin/oidc/callback",
            "oidc_admin_group": "tellybox-admins"}
    assert entrypoint.options_to_env(opts, {}) == {
        "TELLYBOX_OIDC_ISSUER": "https://id.example.org",
        "TELLYBOX_OIDC_CLIENT_ID": "tellybox",
        "TELLYBOX_OIDC_CLIENT_SECRET": "s",
        "TELLYBOX_OIDC_REDIRECT_URI": "https://tellybox.example.org/admin/oidc/callback",
        "TELLYBOX_OIDC_ADMIN_GROUP": "tellybox-admins",
    }


def test_options_to_env_existing_env_wins():
    assert entrypoint.options_to_env({"web_port": 9000}, {"TELLYBOX_WEB_PORT": "1"}) == {}


def test_options_file_applied(tmp_path):
    f = tmp_path / "options.json"
    f.write_text('{"web_port": 9000, "media_base_url": "http://1.2.3.4:9000"}')
    env = {"TELLYBOX_OPTIONS_FILE": str(f)}
    entrypoint.apply_options_file(env)
    assert env["TELLYBOX_WEB_PORT"] == "9000"
    assert env["TELLYBOX_MEDIA_BASE_URL"] == "http://1.2.3.4:9000"


def test_options_file_malformed_or_missing_is_ignored(tmp_path, caplog):
    f = tmp_path / "options.json"
    f.write_text("{nope")
    env = {"TELLYBOX_OPTIONS_FILE": str(f)}
    entrypoint.apply_options_file(env)
    assert env == {"TELLYBOX_OPTIONS_FILE": str(f)}
    assert "ignoring options file" in caplog.text
    env = {"TELLYBOX_OPTIONS_FILE": str(tmp_path / "missing.json")}
    entrypoint.apply_options_file(env)
    assert len(env) == 1


def test_create_dirs(tmp_path):
    env = {
        "TELLYBOX_DATA_DIR": str(tmp_path / "a" / "data"),
        "TELLYBOX_MEDIA_DIR": str(tmp_path / "m" / "x"),
    }
    entrypoint.create_dirs(env)
    assert (tmp_path / "a" / "data").is_dir() and (tmp_path / "m" / "x").is_dir()


def test_main_exports_options_to_exec(monkeypatch, tmp_path, spy):
    f = tmp_path / "options.json"
    f.write_text('{"web_port": 9123}')
    monkeypatch.setattr(os, "geteuid", lambda: 1500)
    monkeypatch.setenv("TELLYBOX_OPTIONS_FILE", str(f))
    monkeypatch.setattr(os, "environ", dict(os.environ))
    monkeypatch.delenv("TELLYBOX_WEB_PORT", raising=False)
    entrypoint.main(["x"])
    assert os.environ["TELLYBOX_WEB_PORT"] == "9123"
