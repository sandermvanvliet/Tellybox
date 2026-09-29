"""Shared fixtures for the kid app: a seeded library and a fake cast client."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from tellybox import db, library, store
from tellybox.config import Config
from tellybox.web.cast_client import CastNotFound, CastUnavailable, TimeUp

NOW = datetime(2026, 9, 28, 14, 0, tzinfo=UTC)
PROFILE = 1  # the household profile from the initial migration


def make_config(tmp_path: Path) -> Config:
    return Config(
        db_path=tmp_path / "data" / "tellybox.db",
        media_dir=tmp_path / "media",
        data_dir=tmp_path / "data",
        tz=ZoneInfo("Europe/Amsterdam"),
        web_host="127.0.0.1",
        web_port=8080,
        cast_api_host="127.0.0.1",
        cast_api_port=8081,
        media_base_url="http://127.0.0.1:8080",
        secret=b"test-secret",
    )


def cast_state(
    *,
    connection: str = "CONNECTED",
    remaining_s: float | None = 1800,
    used_s: float = 1800,
    extra_s: float = 0,
    unlimited: bool = False,
    time_up: bool = False,
    now_playing: dict | None = None,
    profiles: list[dict] | None = None,
) -> dict:
    """A cast service state snapshot, shaped like CastController.state()."""
    if profiles is None:
        profiles = [{"profile_id": PROFILE, "day": "2026-09-28", "used_s": used_s, "extra_s": extra_s,
                     "unlimited": unlimited, "blocked": False, "remaining_s": remaining_s,
                     "can_start": not time_up, "reason": "allowance" if time_up else None,
                     "session_elapsed_s": None, "watching": now_playing is not None}]
    return {
        "connection": connection,
        "device": {"uuid": "5f0c2a17", "name": "Living Room TV"},
        "now_playing": now_playing,
        "timer": {"remaining_s": remaining_s, "can_start": not time_up, "action": "continue", "reason": None,
                  "grace_deadline": None, "session_started_at": None, "session_elapsed_s": None,
                  "next_reset": "2026-09-29T02:00:00+00:00", "profiles": profiles},
        "time_up": time_up,
    }


def playing(episode_id: int, show_id: int, state: str = "playing", title: str = "Ep",
            profile_ids: list[int] | None = None) -> dict:
    return {"episode_id": episode_id, "show_id": show_id, "title": title, "state": state,
            "position_s": 12, "duration_s": 600, "profile_ids": [PROFILE] if profile_ids is None else profile_ids}


class FakeCast:
    """Stands in for CastClient. `mode` scripts the next command's outcome."""

    def __init__(self, state: dict | None = None) -> None:
        self.current = state or cast_state()
        self.mode = "ok"  # ok | time_up | down | not_found | invalid
        self.calls: list[tuple] = []
        self.streams: list = []  # for events(): each item is a list of states or an exception
        self.events_calls = 0
        self.devices_result: dict | None = None  # scripted result for devices()

    def _check(self) -> None:
        if self.mode == "down":
            raise CastUnavailable("cast service down")

    async def state(self) -> dict:
        self.calls.append(("state",))
        self._check()
        return self.current

    async def play(self, episode_id: int, profile_ids: list[int]) -> dict:
        self.calls.append(("play", episode_id, list(profile_ids)))
        self._check()
        if self.mode == "time_up":
            raise TimeUp(cast_state(remaining_s=0, used_s=3600, time_up=True))
        if self.mode == "not_found":
            raise CastNotFound(episode_id)
        return cast_state(now_playing=playing(episode_id, 0, "loading", profile_ids=sorted(profile_ids)))

    async def pause(self) -> dict:
        self.calls.append(("pause",))
        self._check()
        return self.current

    async def resume(self) -> dict:
        self.calls.append(("resume",))
        self._check()
        return self.current

    async def stop(self) -> dict:
        self.calls.append(("stop",))
        self._check()
        return self.current

    async def override(self, kind: str, value: int | None = None, profile_ids: list[int] | None = None,
                       source: str | None = None) -> dict:
        self.calls.append(("override", kind, value, None if profile_ids is None else list(profile_ids), source))
        self._check()
        if self.mode == "invalid":
            raise ValueError(f"invalid override {kind!r}")
        return self.current

    async def devices(self) -> dict:
        self.calls.append(("devices",))
        self._check()
        return self.devices_result if self.devices_result is not None else {"selected": None, "devices": []}

    async def select_device(self, uuid: str) -> dict:
        self.calls.append(("select_device", uuid))
        self._check()
        if self.mode == "not_found":
            raise CastNotFound(uuid)
        return self.current

    async def events(self):
        self.events_calls += 1
        item = self.streams.pop(0) if self.streams else CastUnavailable("no more scripted streams")
        if isinstance(item, BaseException):
            raise item
        for s in item:
            yield s

    async def aclose(self) -> None:
        pass


@pytest.fixture
def config(tmp_path) -> Config:
    config = make_config(tmp_path)
    config.media_dir.mkdir(parents=True)
    (tmp_path / "outside.jpg").write_bytes(b"outside")
    return config


def _thumb(config: Config, rel: str) -> str:
    path = config.media_dir / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"JPEG:" + rel.encode())
    return rel


@pytest.fixture
def lib(config):
    """Seeded library.

    Bravo (sort 0, artwork): b1, b2, b3
    Alpha (sort 1, no artwork): a1, a2, a3 (hidden/held), a4
    Hidden (hidden show): h1
    Empty (only a hidden episode): e1
    """
    conn = db.open_db(":memory:")

    def show(name, sort_order, *, hidden=False, artwork=None):
        sid = library.create_show(conn, name, now=NOW)
        conn.execute("UPDATE show SET sort_order = ?, hidden = ?, artwork_path = ? WHERE id = ?",
                     (sort_order, int(hidden), artwork, sid))
        return sid

    def ep(sid, key, *, hidden=False, thumb=True, duration=600.0):
        rel = _thumb(config, f"thumbs/{key}.jpg") if thumb else None
        return library.add_episode(conn, sid, f"Title {key}", f"eps/{key}.mp4", now=NOW, duration_s=duration,
                                   thumbnail_path=rel, hidden=hidden)

    alpha = show("Alpha", 1)
    bravo = show("Bravo", 0, artwork=_thumb(config, "art/bravo.jpg"))
    hidden = show("Hidden", 2, hidden=True, artwork=_thumb(config, "art/hidden.jpg"))
    empty = show("Empty", 3)
    ids = SimpleNamespace(
        alpha=alpha, bravo=bravo, hidden=hidden, empty=empty,
        b1=ep(bravo, "b1"), b2=ep(bravo, "b2"), b3=ep(bravo, "b3", duration=None),
        a1=ep(alpha, "a1"), a2=ep(alpha, "a2"), a3=ep(alpha, "a3", hidden=True), a4=ep(alpha, "a4"),
        h1=ep(hidden, "h1"), e1=ep(empty, "e1", hidden=True),
    )
    yield conn, ids
    conn.close()


def position(conn, episode_id: int, pos: float, *, finished: bool = False, minutes: int = 0, profile: int = PROFILE):
    store.save_position(conn, [profile], episode_id, pos, finished, NOW + timedelta(minutes=minutes))


# Helpers exposed as fixtures (test modules can't import conftest reliably).
@pytest.fixture
def mkstate():
    return cast_state


@pytest.fixture
def mkplaying():
    return playing


@pytest.fixture
def fake_cast() -> FakeCast:
    return FakeCast()


@pytest.fixture
def pos(lib):
    conn, _ = lib
    return lambda *a, **kw: position(conn, *a, **kw)
