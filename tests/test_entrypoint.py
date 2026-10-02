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
    assert spy["chown"] == [["chown", "-R", f"{os.getuid() + 1}:{os.getgid()}", str(tmp_path)]]
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
