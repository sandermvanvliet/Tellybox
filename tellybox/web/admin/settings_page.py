"""Settings `/admin/settings` (AD-2, PB-1): per-profile allowance/mode/max session,
global reset time/grace cap/session break, and the Chromecast picker.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, time

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, Response
from starlette.datastructures import FormData

from tellybox import sponsorblock
from tellybox.library import _transaction
from tellybox.i18n import _
from tellybox.timer.models import CountingMode
from tellybox.web.admin.common import AdminContext, render, see_other
from tellybox.web.cast_client import CastNotFound, CastUnavailable


def _parse_minutes(raw: str | None, lo: int, hi: int) -> tuple[int | None, str | None]:
    raw = (raw or "").strip()
    try:
        n = int(raw)
    except ValueError:
        return None, _("Enter a whole number of minutes.")
    if not (lo <= n <= hi):
        return None, _("Enter a number between %(lo)d and %(hi)d.") % {"lo": lo, "hi": hi}
    return n, None


def _parse_time(raw: str | None) -> time | None:
    try:
        return datetime.strptime((raw or "").strip(), "%H:%M").time()
    except ValueError:
        return None


def create_router(ctx: AdminContext) -> APIRouter:
    router = APIRouter()
    conn = ctx.conn

    def profiles() -> list[sqlite3.Row]:
        return conn.execute(
            "SELECT id, name, avatar, picture_path, daily_allowance_min, counting_mode, max_session_min"
            " FROM profile ORDER BY sort_order, id"
        ).fetchall()

    def global_settings() -> sqlite3.Row:
        return conn.execute("SELECT reset_time, grace_cap_min, session_break_min FROM settings WHERE id = 1").fetchone()

    def default_values() -> dict[str, str]:
        values: dict[str, str] = {}
        for p in profiles():
            values[f"profile_{p['id']}_allowance_min"] = str(p["daily_allowance_min"])
            values[f"profile_{p['id']}_counting_mode"] = p["counting_mode"]
            values[f"profile_{p['id']}_max_session_min"] = str(p["max_session_min"])
        s = global_settings()
        values["reset_time"] = s["reset_time"]
        values["grace_cap_min"] = str(s["grace_cap_min"])
        values["session_break_min"] = str(s["session_break_min"])
        return values

    def devices_context() -> dict:
        selected = conn.execute("SELECT uuid FROM cast_device WHERE selected = 1").fetchone()
        rows = conn.execute(
            "SELECT uuid, name, host, model, last_seen_at FROM cast_device ORDER BY name, uuid"
        ).fetchall()
        return {
            "known_devices": [dict(r) for r in rows],
            "selected_uuid": selected["uuid"] if selected else None,
        }

    def sb_selected() -> list[str]:
        row = conn.execute("SELECT sponsorblock_categories FROM settings WHERE id = 1").fetchone()
        return sponsorblock.parse_csv(row["sponsorblock_categories"])

    def page_context(**overrides) -> dict:
        data = {"profiles": profiles(), "values": default_values(), "errors": {}, "sb_selected": sb_selected(),
                **devices_context()}
        data.update(overrides)
        return data

    @router.get("/admin/settings")
    def settings_page(request: Request) -> HTMLResponse:
        return render(request, "settings.html", nav="settings", **page_context())

    @router.post("/admin/settings")
    async def save_settings(request: Request) -> Response:
        form: FormData = await request.form()
        values = {k: str(v) for k, v in form.items()}
        errors: dict[str, str] = {}

        parsed_profiles: list[tuple[int, int, str, int]] = []
        for p in profiles():
            pid = p["id"]
            allowance, err = _parse_minutes(form.get(f"profile_{pid}_allowance_min"), 1, 1440)
            if err:
                errors[f"profile_{pid}_allowance_min"] = err
            mode = form.get(f"profile_{pid}_counting_mode")
            if mode not in (CountingMode.IGNORE_PAUSES.value, CountingMode.WALL_CLOCK.value):
                errors[f"profile_{pid}_counting_mode"] = _("Choose a counting mode.")
            max_session, err = _parse_minutes(form.get(f"profile_{pid}_max_session_min"), 1, 1440)
            if err:
                errors[f"profile_{pid}_max_session_min"] = err
            if not err and allowance is not None and mode is not None and max_session is not None:
                parsed_profiles.append((pid, allowance, mode, max_session))

        reset_time = _parse_time(form.get("reset_time"))
        if reset_time is None:
            errors["reset_time"] = _("Enter a time as HH:MM.")
        grace_cap, err = _parse_minutes(form.get("grace_cap_min"), 0, 60)
        if err:
            errors["grace_cap_min"] = err
        session_break, err = _parse_minutes(form.get("session_break_min"), 1, 240)
        if err:
            errors["session_break_min"] = err

        sb_chosen = sponsorblock.parse_csv(",".join(str(v) for v in form.getlist("sponsorblock")))  # SB-2

        if errors or len(parsed_profiles) != len(profiles()):
            return render(request, "settings.html", 422, nav="settings",
                          **page_context(values=values, errors=errors, sb_selected=sb_chosen))

        with _transaction(conn):
            for pid, allowance, mode, max_session in parsed_profiles:
                conn.execute(
                    "UPDATE profile SET daily_allowance_min = ?, counting_mode = ?, max_session_min = ? WHERE id = ?",
                    (allowance, mode, max_session, pid),
                )
            conn.execute(
                "UPDATE settings SET reset_time = ?, grace_cap_min = ?, session_break_min = ? WHERE id = 1",
                (reset_time.strftime("%H:%M"), grace_cap, session_break),
            )
            conn.execute("UPDATE settings SET sponsorblock_categories = ? WHERE id = 1", (sponsorblock.to_csv(sb_chosen),))
        return see_other("/admin/settings", flash=_("Saved. The TV picks this up within 15 s."))

    @router.post("/admin/settings/devices/search")
    async def search_devices(request: Request) -> Response:
        try:
            result = await ctx.cast.devices()
        except CastUnavailable:
            return see_other("/admin/settings", flash=_("Cast service unreachable; couldn't search for devices."))
        found = result.get("devices") or []
        if not found:
            return see_other("/admin/settings", flash=_("No Chromecasts found."))
        names = ", ".join(d.get("name") or d.get("uuid", "?") for d in found)
        return see_other("/admin/settings", flash=_("Found: %(names)s.") % {"names": names})

    @router.post("/admin/settings/devices/select")
    async def select_device(request: Request, uuid: str = Form(...)) -> Response:
        try:
            await ctx.cast.select_device(uuid)
        except CastNotFound:
            return see_other("/admin/settings", flash=_("Unknown device; search again."))
        except CastUnavailable:
            return see_other("/admin/settings", flash=_("Cast service unreachable; nothing was changed."))
        return see_other("/admin/settings", flash=_("Chromecast selected."))

    return router
