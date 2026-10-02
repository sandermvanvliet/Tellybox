"""Kid app API (docs/kid-api.md): library queries, the live KidState and the routes.

No login (NF-1), so everything here only ever exposes visible content: hidden shows,
hidden (incl. held) episodes and episodes of hidden shows are never listed, playable
or served as images (KA-4).
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Awaitable, Callable
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel

from tellybox import library, store
from tellybox.config import Config
from tellybox.web.cast_client import CastNotFound, CastUnavailable, TimeUp
from tellybox.web.hub import KidHub, sse_stream, unreachable

log = logging.getLogger(__name__)

CONTINUE_MAX = 8  # KA-3
RESUME_MIN_S = 10.0  # below this an episode counts as not started (PB-4)
LAST_FIVE_S = 300  # KA-8
PLAYER_STATES = {"loading", "playing", "paused", "buffering"}
IMAGE_CACHE = {"Cache-Control": "max-age=3600"}
PROFILE_IMAGE_CACHE = {"Cache-Control": "no-cache"}

MAX_GROUP = 20  # profile ids in one group (docs/kid-api.md)

_VISIBLE = "e.hidden = 0 AND s.hidden = 0"


# --------------------------------------------------------------------------- profiles and groups (PR-1, PR-2)


def profile_order(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Every profile in the admin's order.

    Note: does not include allowance info; use store.effective_allowance_s() for resolved allowances (A-23).
    """
    return conn.execute(
        "SELECT id, name, picture_path, avatar, ui_mode FROM profile ORDER BY sort_order, id"
    ).fetchall()


def first_profile_id(conn: sqlite3.Connection) -> int:
    """Omitting the group means the first profile (the v1 behaviour with a single profile)."""
    return profile_order(conn)[0]["id"]


def parse_group(conn: sqlite3.Connection, ids: list[int] | None) -> list[int]:
    """A group of 1-20 existing profile ids, duplicates dropped; None means the first profile.

    Raises HTTPException(400, "bad_profiles") for anything else.
    """
    if ids is None:
        return [first_profile_id(conn)]
    if not ids or len(ids) > MAX_GROUP:
        raise HTTPException(400, "bad_profiles")
    ids = list(dict.fromkeys(ids))
    known = {r["id"] for r in profile_order(conn)}
    if not all(i in known for i in ids):
        raise HTTPException(400, "bad_profiles")
    return ids


def parse_group_param(conn: sqlite3.Connection, raw: str | None) -> list[int]:
    """The `?profiles=1,3` query parameter."""
    if raw is None:
        return parse_group(conn, None)
    parts = raw.split(",")
    if len(parts) > MAX_GROUP or not all(p.strip().isdigit() for p in parts):
        raise HTTPException(400, "bad_profiles")
    return parse_group(conn, [int(p) for p in parts])


def _marks(ids: list[int]) -> str:
    return ",".join("?" * len(ids))


def _thumb(episode_id: int) -> str:
    return f"/img/episode/{episode_id}.jpg"


def _tile(row: sqlite3.Row) -> dict:
    pos, duration = row["position_s"], row["duration_s"]
    progress = None if pos is None or not duration else round(min(max(pos / duration, 0.0), 1.0), 3)
    return {"episode_id": row["id"], "show_id": row["show_id"], "thumb": _thumb(row["id"]), "title": row["title"],
            "progress": progress, "finished": bool(row["finished"])}


def _show(row: sqlite3.Row) -> dict:
    return {"show_id": row["id"], "artwork": f"/img/show/{row['id']}.jpg", "title": row["name"]}


# --------------------------------------------------------------------------- queries (KA-3, KA-4, PB-4)


def list_shows(conn: sqlite3.Connection) -> list[dict]:
    """Visible shows with at least one visible episode, in admin order."""
    rows = conn.execute(
        """SELECT s.id, s.name FROM show s
           WHERE s.hidden = 0 AND EXISTS (SELECT 1 FROM episode e WHERE e.show_id = s.id AND e.hidden = 0)
           ORDER BY s.sort_order, s.id"""
    ).fetchall()
    return [_show(r) for r in rows]


def get_show(conn: sqlite3.Connection, show_id: int, profile_ids: list[int] | None = None) -> dict | None:
    row = conn.execute("SELECT id, name FROM show WHERE id = ? AND hidden = 0", (show_id,)).fetchone()
    if row is None:
        return None
    return {**_show(row), "episodes": show_episodes(conn, show_id, profile_ids)}


def _latest_positions(conn: sqlite3.Connection, profile_ids: list[int], where: str, args: tuple) -> dict[int, sqlite3.Row]:
    """episode id -> the most recently updated position among the group's members (PB-4)."""
    rows = conn.execute(
        f"""SELECT p.episode_id, p.position_s, p.finished FROM playback_position p
            JOIN episode e ON e.id = p.episode_id JOIN show s ON s.id = e.show_id
            WHERE p.profile_id IN ({_marks(profile_ids)}) AND {where}
            ORDER BY p.updated_at, p.profile_id""",
        (*profile_ids, *args),
    ).fetchall()
    return {r["episode_id"]: r for r in rows}  # later rows (more recent) win


def _with_position(row: sqlite3.Row, pos: sqlite3.Row | None) -> dict:
    return {**dict(row), "position_s": pos["position_s"] if pos else None, "finished": pos["finished"] if pos else None}


def show_episodes(conn: sqlite3.Connection, show_id: int, profile_ids: list[int] | None = None) -> list[dict]:
    """Visible episodes in episode order, with the group's progress (the most recent position among its members)."""
    profile_ids = parse_group(conn, profile_ids)
    rows = conn.execute(
        f"""SELECT e.id, e.show_id, e.title, e.duration_s FROM episode e JOIN show s ON s.id = e.show_id
            WHERE e.show_id = ? AND {_VISIBLE}
            ORDER BY e.sort_order, e.id""",
        (show_id,),
    ).fetchall()
    positions = _latest_positions(conn, profile_ids, "e.show_id = ?", (show_id,))
    return [_tile(_with_position(r, positions.get(r["id"]))) for r in rows]


def _episode_tile(conn: sqlite3.Connection, episode_id: int, profile_ids: list[int]) -> dict | None:
    row = conn.execute(
        f"""SELECT e.id, e.show_id, e.title, e.duration_s FROM episode e JOIN show s ON s.id = e.show_id
            WHERE e.id = ? AND {_VISIBLE}""",
        (episode_id,),
    ).fetchone()
    if row is None:
        return None
    pos = _latest_positions(conn, profile_ids, "e.id = ?", (episode_id,)).get(episode_id)
    return _tile(_with_position(row, pos))


def continue_watching(conn: sqlite3.Connection, profile_ids: list[int] | None = None, limit: int = CONTINUE_MAX) -> list[dict]:
    """KA-3, PB-4: episodes to resume, plus the next episode of shows whose last watched one was finished.

    For a group the members' lists merge: each episode counts once, with the most recent position
    among the members. Most recently watched first.
    """
    profile_ids = parse_group(conn, profile_ids)
    all_rows = conn.execute(
        f"""SELECT e.id, e.show_id, e.hidden, p.position_s, p.finished, p.updated_at
            FROM playback_position p JOIN episode e ON e.id = p.episode_id JOIN show s ON s.id = e.show_id
            WHERE p.profile_id IN ({_marks(profile_ids)}) AND s.hidden = 0
            ORDER BY p.updated_at DESC, e.id DESC""",
        profile_ids,
    ).fetchall()
    rows, seen_episodes = [], set()
    for r in all_rows:  # newest first: the first row of an episode is the group's latest position
        if r["id"] not in seen_episodes:
            seen_episodes.add(r["id"])
            rows.append(r)
    unfinished = {r["id"] for r in rows if not r["finished"]}
    picks: list[tuple[str, int]] = []  # (kind, episode_id), already in updated_at order
    seen_shows: set[int] = set()
    for r in rows:
        if not r["finished"] and not r["hidden"] and r["position_s"] >= RESUME_MIN_S:
            picks.append(("resume", r["id"]))
        if r["show_id"] in seen_shows:
            continue
        seen_shows.add(r["show_id"])  # only the show's most recently watched episode decides "next"
        if r["finished"]:
            nxt = library.next_episode(conn, r["id"])  # skips hidden episodes
            if nxt is not None and nxt.id not in unfinished:
                picks.append(("next", nxt.id))
    tiles: list[dict] = []
    added: set[int] = set()
    for kind, episode_id in picks:
        if episode_id in added:
            continue
        tile = _episode_tile(conn, episode_id, profile_ids)
        if tile is None:
            continue
        added.add(episode_id)
        tiles.append({**tile, "kind": kind})
        if len(tiles) == limit:
            break
    return tiles


def episode_is_visible(conn: sqlite3.Connection, episode_id: int) -> bool:
    return conn.execute(
        f"SELECT 1 FROM episode e JOIN show s ON s.id = e.show_id WHERE e.id = ? AND {_VISIBLE}", (episode_id,)
    ).fetchone() is not None


def episode_image_paths(conn: sqlite3.Connection, episode_id: int) -> list[str]:
    row = conn.execute(
        f"SELECT e.thumbnail_path FROM episode e JOIN show s ON s.id = e.show_id WHERE e.id = ? AND {_VISIBLE}",
        (episode_id,),
    ).fetchone()
    return [row[0]] if row and row[0] else []


def show_image_paths(conn: sqlite3.Connection, show_id: int) -> list[str]:
    """Candidates in order: the show's artwork, then visible episodes' thumbnails (episode order).

    Empty for hidden shows and shows without visible episodes, which the kid never sees.
    """
    show = conn.execute("SELECT artwork_path FROM show WHERE id = ? AND hidden = 0", (show_id,)).fetchone()
    if show is None:
        return []
    thumbs = [r[0] for r in conn.execute(
        "SELECT thumbnail_path FROM episode WHERE show_id = ? AND hidden = 0 ORDER BY sort_order, id", (show_id,)
    )]
    if not thumbs:
        return []
    return [p for p in [show["artwork_path"], *thumbs] if p]


def profile_image_paths(conn: sqlite3.Connection, profile_id: int) -> list[str]:
    row = conn.execute("SELECT picture_path FROM profile WHERE id = ?", (profile_id,)).fetchone()
    return [row[0]] if row and row[0] else []


# --------------------------------------------------------------------------- KidState (KA-6..KA-9)


def _clamp_fraction(left: float, total: float) -> float:
    return min(max(left / total, 0.0), 1.0) if total > 0 else 0.0


def _profile_left_s(entry: dict, allowance_s: float | None) -> float | None:
    """Seconds left for one profile in the cast state; None = unlimited.

    A-23: per-profile limits; allowance_s can be None for unlimited.
    """
    if allowance_s is None or entry.get("unlimited"):
        return None
    if entry.get("remaining_s") is not None:
        return entry["remaining_s"]
    return max(0.0, allowance_s + entry.get("extra_s", 0) - entry.get("used_s", 0))


def _profile_state(entry: dict | None, allowance_s: float | None) -> dict:
    """One profile's slice of the KidState: how much of its day is left (KA-8, KA-9).

    A-23: per-profile limits; allowance_s can be None for unlimited.
    """
    if entry is None:  # no timer data (yet): a full day
        if allowance_s is None:
            return {"fraction_left": None, "last_five": False, "unlimited": True, "time_up": False}
        return {"fraction_left": 1.0, "last_five": False, "unlimited": False, "time_up": False}
    left = _profile_left_s(entry, allowance_s)
    time_up = not entry.get("can_start", True)
    if left is None:
        return {"fraction_left": None, "last_five": False, "unlimited": True, "time_up": time_up}
    return {"fraction_left": round(_clamp_fraction(left, allowance_s + entry.get("extra_s", 0)), 3),
            "last_five": left <= LAST_FIVE_S, "unlimited": False, "time_up": time_up}


def _fraction_left(conn: sqlite3.Connection, timer: dict, remaining_s: float) -> float:
    allowance = {r["id"]: store.effective_allowance_s(conn, r["id"]) for r in profile_order(conn)}  # A-23

    entries = [p for p in timer.get("profiles") or [] if p.get("profile_id") in allowance]
    watchers = [p for p in entries if p.get("watching")]
    limited = [p for p in (watchers or entries) if not p.get("unlimited") and allowance[p["profile_id"]] is not None]

    if len(limited) > 1:  # several profiles watching; the one with the least left decides
        return min(_clamp_fraction(_profile_left_s(p, allowance[p["profile_id"]]), allowance[p["profile_id"]] + p["extra_s"])
                   for p in limited)
    if limited:
        return _clamp_fraction(remaining_s, allowance[limited[0]["profile_id"]] + limited[0]["extra_s"])
    first_id = first_profile_id(conn)
    first_allow = allowance.get(first_id, 0.0)
    return _clamp_fraction(remaining_s, first_allow) if first_allow is not None else 0.0


def kid_state(conn: sqlite3.Connection, cast: dict) -> dict:
    """Reduce the cast service's state to what the kid screen shows. The only device detail is the TV's
    name (KA-11); the reader UI shows it, the icon UI ignores it."""
    timer = cast.get("timer") or {}
    remaining = timer.get("remaining_s")
    if remaining is None:
        sky = {"fraction_left": None, "last_five": False, "unlimited": True}
    else:
        sky = {"fraction_left": round(_fraction_left(conn, timer, remaining), 3),
               "last_five": remaining <= LAST_FIVE_S, "unlimited": False}
    np = cast.get("now_playing")
    now_playing = None
    if np:
        now_playing = {"episode_id": np["episode_id"], "show_id": np["show_id"], "thumb": _thumb(np["episode_id"]),
                       "title": np.get("title") or "",
                       "state": np.get("state") if np.get("state") in PLAYER_STATES else "loading"}
    entries = {p["profile_id"]: p for p in timer.get("profiles") or []}
    days = [p["day"] for p in entries.values() if p.get("day")]
    profiles_row = profile_order(conn)
    allowances = {r["id"]: store.effective_allowance_s(conn, r["id"]) for r in profiles_row}
    device = cast.get("device") or {}
    return {
        "tv": "ok" if cast.get("connection") == "CONNECTED" else "unreachable",
        "device_name": device.get("name") or None,  # KA-11, PB-6
        "now_playing": now_playing,
        "sky": sky,
        "time_up": bool(cast.get("time_up")),
        "watching": sorted(np.get("profile_ids") or []) if np else [],
        "profiles": {str(r["id"]): _profile_state(entries.get(r["id"]), allowances[r["id"]])
                     for r in profiles_row},
        "day": days[0] if days else None,
    }


# --------------------------------------------------------------------------- routes


class PlayRequest(BaseModel):
    episode_id: int
    profile_ids: list[int] | None = None  # omitted = the first profile


def create_router(config: Config, conn: sqlite3.Connection, cast, hub: KidHub,
                  resolve: Callable[[Path, str], Path | None]) -> APIRouter:
    # All handlers are async so the shared sqlite connection is only used from the event loop.
    router = APIRouter()

    def reduce(state: dict, status: int = 200) -> JSONResponse:
        return JSONResponse(kid_state(conn, state), status_code=status)

    def not_found() -> HTTPException:
        return HTTPException(404, "not_found")

    async def command(name: str, call: Callable[[], Awaitable[dict]]) -> JSONResponse:
        try:
            return reduce(await call())
        except TimeUp as exc:  # KA-9
            return reduce(exc.state, 409)
        except CastUnavailable as exc:
            log.warning("kid %s: TV unreachable: %s", name, exc)
            return JSONResponse(unreachable(hub.state), status_code=503)

    @router.get("/api/kid/profiles")
    async def profiles() -> list[dict]:  # PR-1, PR-2
        try:
            reduced = kid_state(conn, await cast.state())["profiles"]
        except CastUnavailable:
            reduced = hub.state.get("profiles") or {}
        blank = {"fraction_left": None, "last_five": False, "unlimited": False, "time_up": False}
        return [{"profile_id": r["id"], "name": r["name"],
                 "picture": f"/img/profile/{r['id']}.jpg" if r["picture_path"] else None,
                 "avatar": r["avatar"], "ui_mode": r["ui_mode"], **(reduced.get(str(r["id"])) or blank)}
                for r in profile_order(conn)]

    @router.get("/api/kid/home")
    async def home(profiles: str | None = Query(None)) -> dict:
        group = parse_group_param(conn, profiles)
        return {"continue": continue_watching(conn, group), "shows": list_shows(conn)}

    @router.get("/api/kid/shows/{show_id}")
    async def show(show_id: int, profiles: str | None = Query(None)) -> dict:
        group = parse_group_param(conn, profiles)
        page = get_show(conn, show_id, group)
        if page is None:
            raise not_found()
        return page

    @router.get("/api/kid/state")
    async def state() -> JSONResponse:
        try:
            return reduce(await cast.state())
        except CastUnavailable:
            return JSONResponse(unreachable(hub.state))

    @router.get("/api/kid/events")
    async def events() -> StreamingResponse:
        return StreamingResponse(sse_stream(hub), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @router.post("/api/kid/play")
    async def play(req: PlayRequest) -> JSONResponse:  # KA-5, PR-2
        group = parse_group(conn, req.profile_ids)
        if not episode_is_visible(conn, req.episode_id):
            raise not_found()
        try:
            return await command("play", lambda: cast.play(req.episode_id, group))
        except CastNotFound:
            raise not_found() from None

    @router.post("/api/kid/pause")
    async def pause() -> JSONResponse:  # KA-6
        return await command("pause", cast.pause)

    @router.post("/api/kid/resume")
    async def resume() -> JSONResponse:
        return await command("resume", cast.resume)

    def image(candidates: list[str]) -> FileResponse:
        for rel in candidates:
            path = resolve(config.media_dir, rel)
            if path is not None:
                return FileResponse(path, headers=IMAGE_CACHE)
        raise not_found()

    @router.get("/img/profile/{profile_id}.jpg")
    async def profile_image(profile_id: int) -> FileResponse:
        # The URL stays the same when the photo is replaced, so revalidate instead of caching for an hour.
        for rel in profile_image_paths(conn, profile_id):
            path = resolve(config.media_dir, rel)
            if path is not None:
                return FileResponse(path, headers=PROFILE_IMAGE_CACHE)
        raise not_found()

    @router.get("/img/episode/{episode_id}.jpg")
    async def episode_image(episode_id: int) -> FileResponse:
        return image(episode_image_paths(conn, episode_id))

    @router.get("/img/show/{show_id}.jpg")
    async def show_image(show_id: int) -> FileResponse:
        return image(show_image_paths(conn, show_id))

    return router
