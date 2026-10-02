import os
import stat as statmod
from types import SimpleNamespace

import pytest

from tellybox import privileges


def test_defaults():
    assert privileges.target_ids({}) == (1500, 1500)
    assert privileges.target_ids({"PUID": "", "PGID": " "}) == (1500, 1500)


def test_custom_and_explicit_zero():
    assert privileges.target_ids({"PUID": "1000", "PGID": "100"}) == (1000, 100)
    assert privileges.target_ids({"PUID": "0"}) == (0, 1500)


@pytest.mark.parametrize("bad", ["abc", "-1", "1.5"])
def test_invalid(bad):
    with pytest.raises(ValueError):
        privileges.target_ids({"PUID": bad})


def _st(uid, gid, mode=statmod.S_IFDIR):
    return SimpleNamespace(st_uid=uid, st_gid=gid, st_mode=mode)


def test_dirs_needing_chown():
    table = {"/a": _st(0, 0), "/b": _st(1500, 1500), "/c": _st(1500, 0), "/f": _st(0, 0, statmod.S_IFREG)}

    def fake(p):
        try:
            return table[str(p)]
        except KeyError:
            raise FileNotFoundError(p) from None

    got = privileges.dirs_needing_chown(["/a", "/b", "/c", "/missing", "/f"], 1500, 1500, stat=fake)
    assert [str(p) for p in got] == ["/a", "/c"]


def test_drop_root_noop_when_not_root(monkeypatch):
    monkeypatch.setattr(os, "geteuid", lambda: 1000)
    called = []
    monkeypatch.setattr(os, "setuid", lambda u: called.append(u))
    assert privileges.drop_root(1500, 1500) is False
    assert called == []


def test_drop_root_order(monkeypatch):
    calls = []

    def no_user(uid):
        raise KeyError(uid)

    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr(os, "setgroups", lambda g: calls.append(("groups", g)))
    monkeypatch.setattr(os, "setgid", lambda g: calls.append(("gid", g)))
    monkeypatch.setattr(os, "setuid", lambda u: calls.append(("uid", u)))
    monkeypatch.setattr(privileges.pwd, "getpwuid", no_user)
    monkeypatch.setenv("HOME", "/root")
    assert privileges.drop_root(1234, 4321) is True
    assert calls == [("groups", []), ("gid", 4321), ("uid", 1234)]
    assert os.environ["HOME"] == "/tmp"
