"""Subscriptions `/admin/subscriptions` and the approval inbox `/admin/inbox` (CS-1..CS-9, A-31..A-36).

Server-rendered like the other admin pages; the YouTube side goes through `ctx.ytdlp` (a ChannelLister, so tests
pass a fake) and the decisions through `tellybox.subscriptions`. Nothing is downloaded until the admin approves.

The backlog of a new subscription (CS-1) is listed page by page. The entries of the pages shown are kept in memory
per subscription, so the form only carries video ids and each one is checked against what the page offered.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

from tellybox import ingest, library, subscriptions
from tellybox.i18n import _, ngettext
from tellybox.web.admin.add import playlist_flash
from tellybox.web.admin.common import AdminContext, render, see_other
from tellybox.ytdlp import ChannelEntry, VideoInfo, YtDlpError

BACKLOG_CAP = 20  # subscriptions whose listed backlog entries are kept in memory


def _int_or_none(raw: str | None) -> int | None:
    try:
        return int(raw) if raw not in (None, "") else None
    except ValueError:
        return None


def create_router(ctx: AdminContext) -> APIRouter:
    router = APIRouter()
    conn = ctx.conn
    # subscription id -> {youtube_id: entry} of every backlog page shown; per router, so per app.
    offered: dict[int, dict[str, ChannelEntry]] = {}

    def lister():
        return ctx.ytdlp

    def now() -> datetime:
        return ctx.clock.now()

    def error_message(exc: YtDlpError) -> str:
        return _("%(error)s It may work later.") % {"error": exc.message} if exc.retryable else exc.message

    # ------------------------------------------------------------------ subscriptions

    def show_names() -> dict[int, str]:
        return {s.id: s.name for s in library.list_shows(conn)}

    def subscriptions_page(request: Request, status_code: int = 200, **extra) -> HTMLResponse:
        return render(
            request, "subscriptions.html", status_code=status_code, nav="subscriptions", tz=ctx.config.tz,
            subs=subscriptions.list_subscriptions(conn, now=now()), shows=library.list_shows(conn),
            show_names=show_names(), url=extra.pop("url", ""), **extra,
        )

    @router.get("/admin/subscriptions")
    def subscriptions_list(request: Request, url: str = "") -> HTMLResponse:
        return subscriptions_page(request, url=url.strip())

    @router.post("/admin/subscriptions")
    def subscribe(request: Request, url: str = Form(""), show_id: str = Form(""), shorts: str = Form("")) -> Response:
        url = url.strip()
        chosen = _int_or_none(show_id)
        if show_id and (chosen is None or library.get_show(conn, chosen) is None):
            return subscriptions_page(request, 422, url=url, error=_("Choose one of the existing shows."))
        try:
            sub = subscriptions.subscribe(conn, lister(), url, show_id=chosen, include_shorts=bool(shorts), now=now())
        except subscriptions.AlreadySubscribed:
            return subscriptions_page(request, 422, url=url, error=_("You are already subscribed to that channel."))
        except YtDlpError as exc:
            return subscriptions_page(request, 422, url=url, error=error_message(exc))
        return see_other(f"/admin/subscriptions/{sub.id}/backlog",
                         flash=_("Subscribed to %(channel)s. Pick any older videos you want now.")
                         % {"channel": sub.channel_name})

    # ------------------------------------------------------------------ backlog (CS-1)

    def remember(sub_id: int, entries: list[ChannelEntry]) -> None:
        store = offered.setdefault(sub_id, {})
        store.update({e.youtube_id: e for e in entries})
        while len(offered) > BACKLOG_CAP:
            del offered[next(iter(offered))]

    @router.get("/admin/subscriptions/{sub_id}/backlog")
    def backlog(request: Request, sub_id: int, offset: int = 0, tab: str = "videos", fragment: str = "") -> Response:
        sub = subscriptions.get_subscription(conn, sub_id, now=now())
        if sub is None:
            return see_other("/admin/subscriptions", flash=_("That subscription no longer exists."))
        tab = "shorts" if tab == "shorts" and sub.include_shorts else "videos"
        offset = max(offset, 0)
        try:
            page = subscriptions.list_backlog(conn, lister(), sub, offset, tab=tab)
        except YtDlpError as exc:
            return subscriptions_page(request, 502, error=error_message(exc))
        remember(sub.id, page.entries)
        context = dict(sub=sub, page=page, tab=tab, offset=offset, tz=ctx.config.tz)
        if fragment:
            response = render(request, "_backlog_items.html", **context)
            response.headers["X-Next-Offset"] = "" if page.next_offset is None else str(page.next_offset)
            return response
        return render(request, "backlog.html", nav="subscriptions", **context)

    @router.post("/admin/subscriptions/{sub_id}/backlog")
    def backlog_approve(request: Request, sub_id: int, video: list[str] = Form([])) -> Response:
        sub = subscriptions.get_subscription(conn, sub_id, now=now())
        if sub is None:
            return see_other("/admin/subscriptions", flash=_("That subscription no longer exists."))
        known = offered.get(sub_id, {})
        if not video or any(v not in known for v in video):
            return see_other(f"/admin/subscriptions/{sub_id}/backlog",
                             flash=_("That selection doesn't match the list; try again."))
        added = skipped = 0
        for youtube_id in dict.fromkeys(video):
            entry = known[youtube_id]
            info = VideoInfo(
                youtube_id=entry.youtube_id, url=entry.url, title=entry.title, channel_id=sub.channel_id,
                channel_name=sub.channel_name, duration_s=entry.duration_s, thumbnail_url=entry.thumbnail_url,
                chapters=[], is_live=False,
            )
            try:  # the same call an inbox approval makes (CS-9: the subscription's show, or the channel's)
                ingest.add(conn, info, publish=True, now=now(), show_id=sub.show_id)
            except ingest.AlreadyAdded:
                skipped += 1
            else:
                added += 1
        return see_other("/admin/jobs", flash=playlist_flash(added, skipped, held=False))

    # ------------------------------------------------------------------ pause, resume, remove, check now

    def with_subscription(sub_id: int, action) -> Response:
        try:
            return action()
        except KeyError:
            return see_other("/admin/subscriptions", flash=_("That subscription no longer exists."))

    @router.post("/admin/subscriptions/check-all")
    def check_all() -> Response:
        count = subscriptions.request_check(conn, None, now=now())
        return see_other("/admin/subscriptions", flash=ngettext(
            "Checking %(num)d subscription now.", "Checking %(num)d subscriptions now.", count) % {"num": count})

    @router.post("/admin/subscriptions/{sub_id}/check")
    def check_now(sub_id: int) -> Response:
        def act() -> Response:
            subscriptions.request_check(conn, sub_id, now=now())
            return see_other("/admin/subscriptions", flash=_("Checking now. New videos show up in the inbox."))
        return with_subscription(sub_id, act)

    @router.post("/admin/subscriptions/{sub_id}/pause")
    def pause(sub_id: int) -> Response:
        def act() -> Response:
            subscriptions.pause(conn, sub_id)
            return see_other("/admin/subscriptions", flash=_("Paused. The inbox stays as it is."))
        return with_subscription(sub_id, act)

    @router.post("/admin/subscriptions/{sub_id}/resume")
    def resume(sub_id: int) -> Response:
        def act() -> Response:
            subscriptions.resume(conn, sub_id)
            return see_other("/admin/subscriptions", flash=_("Resumed."))
        return with_subscription(sub_id, act)

    @router.post("/admin/subscriptions/{sub_id}/remove")
    def remove(sub_id: int) -> Response:
        def act() -> Response:
            subscriptions.remove(conn, sub_id)
            offered.pop(sub_id, None)
            return see_other("/admin/subscriptions",
                             flash=_("Subscription removed. The show and its episodes stay."))
        return with_subscription(sub_id, act)

    # ------------------------------------------------------------------ inbox (CS-6, CS-8)

    def inbox_url(channel: int | None, tab: str = "pending") -> str:
        query = [f"channel={channel}"] if channel is not None else []
        if tab == "rejected":
            query.append("tab=rejected")
        return "/admin/inbox" + ("?" + "&".join(query) if query else "")

    def back(channel: str, tab: str = "pending", flash: str | None = None) -> Response:
        return see_other(inbox_url(_int_or_none(channel), tab), flash=flash)

    @router.get("/admin/inbox")
    def inbox(request: Request, channel: str = "", tab: str = "pending") -> HTMLResponse:
        tab = "rejected" if tab == "rejected" else "pending"
        channel_id = _int_or_none(channel)
        return render(
            request, "inbox.html", nav="inbox", tz=ctx.config.tz, tab=tab, channel=channel_id,
            items=subscriptions.list_inbox(conn, status=tab, subscription_id=channel_id),
            subs=subscriptions.list_subscriptions(conn, now=now()),
            pending_total=subscriptions.inbox_counts(conn, now())["pending"],
            rejected_total=len(subscriptions.list_inbox(conn, status="rejected")),
            not_downloadable=subscriptions.WARNING_NOT_DOWNLOADABLE,
        )

    @router.get("/admin/inbox/count")
    def inbox_count() -> JSONResponse:
        """The nav badge polls this."""
        return JSONResponse({"pending": subscriptions.inbox_counts(conn, now())["pending"]},
                            headers={"Cache-Control": "no-store"})

    def decision_flash(approved: int, skipped: int) -> str:
        text = ngettext("Approved %(num)d video. It downloads now and shows up when it is ready.",
                        "Approved %(num)d videos. They download now and show up when they are ready.", approved)
        text = text % {"num": approved}
        if skipped:
            text += " " + ngettext("%(num)d was skipped.", "%(num)d were skipped.", skipped) % {"num": skipped}
        return text

    @router.post("/admin/inbox/{item_id}/approve")
    def approve(item_id: int, channel: str = Form("")) -> Response:
        try:
            subscriptions.approve(conn, item_id, now=now())
        except KeyError:
            return back(channel, flash=_("That video is no longer in the inbox."))
        except subscriptions.ItemNotPending:
            return back(channel, flash=_("That video was already decided."))
        except ingest.AlreadyAdded:
            return back(channel, flash=_("That video is already in the library."))
        return back(channel, flash=decision_flash(1, 0))

    @router.post("/admin/inbox/{item_id}/reject")
    def reject(item_id: int, channel: str = Form("")) -> Response:
        try:
            subscriptions.reject(conn, item_id, now=now())
        except KeyError:
            return back(channel, flash=_("That video is no longer in the inbox."))
        except subscriptions.ItemNotPending:
            return back(channel, flash=_("That video was already decided."))
        return back(channel, flash=_("Rejected. You can undo this on the Rejected tab."))

    @router.post("/admin/inbox/{item_id}/undo")
    def undo(item_id: int, channel: str = Form("")) -> Response:
        try:
            subscriptions.undo_reject(conn, item_id, now=now())
        except KeyError:
            return back(channel, "rejected", _("That video is no longer in the inbox."))
        except subscriptions.ItemNotPending:
            return back(channel, "rejected", _("That video is not rejected."))
        return back(channel, "rejected", _("Back in the inbox."))

    @router.post("/admin/inbox/bulk")
    def bulk(action: str = Form(""), item: list[int] = Form([]), channel: str = Form("")) -> Response:
        if action == "reject_all":  # every pending item in the current view, not just the ticked ones
            item = [i.id for i in subscriptions.list_inbox(conn, subscription_id=_int_or_none(channel))]
            action = "reject"
        if action not in ("approve", "reject"):
            return back(channel, flash=_("Choose what to do with the selection."))
        if not item:
            return back(channel, flash=_("Tick at least one video first."))
        if action == "approve":
            result = subscriptions.bulk_approve(conn, item, now=now())
            return back(channel, flash=decision_flash(len(result.approved), len(result.skipped)))
        rejected = subscriptions.bulk_reject(conn, item, now=now())
        return back(channel, flash=ngettext("Rejected %(num)d video. You can undo this on the Rejected tab.",
                                            "Rejected %(num)d videos. You can undo this on the Rejected tab.",
                                            rejected) % {"num": rejected})

    return router

