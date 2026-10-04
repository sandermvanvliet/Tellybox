# Step 11 handoff: briefs for subagents A and B

Read first: `CLAUDE.md`, `docs/plans/step11-subscriptions.md` (decisions, spike result), the PRD's CS-1..CS-9, HA-9 and A-31..A-36.

## The contract (already committed; do not change its behaviour)

- Migrations `017_subscriptions.sql` and `018_subscription_check_request.sql`.
- `tellybox/subscriptions.py`: `subscribe`, `list_backlog`, `pause`, `resume`, `remove`, `check`, `approve`, `reject`, `undo_reject`, `bulk_approve`, `bulk_reject`, `list_inbox`, `list_subscriptions`, `get_subscription`, `get_item`, `inbox_counts`, `get_check_hours`, `set_check_hours`, and for "Check now": `request_check(conn, subscription_id | None, *, now)` and `due_subscriptions(conn, now)`. `check` clears a pending request, also when it fails.
- `tellybox/ytdlp.py`: `YtDlp.list_channel`, `YtDlp.video_status`, the `ChannelLister` protocol. Tests use `tests/channel_fakes.py` (`FakeChannelLister`).
- `ingest.add(..., show_id=)` forces the show.
- The English text "may not be downloadable" is stored in `inbox_item.warning`; compare it with `subscriptions.WARNING_NOT_DOWNLOADABLE` and translate it when showing it.

Rules for both: tests first; no network in tests; do not commit (the controller reviews and merges); do not edit `docs/PRD.md`, `docs/plans/` or the migrations; run the relevant tests and report any failure unmasked. Use `/home/sander/Projects/Tellybox/.venv/bin/python -m pytest`.

## A: worker (periodic check)

- In `tellybox/worker/__init__.py`, give `Worker.step()` a subscription check next to its purge and SponsorBlock re-check: when `subscriptions.due_subscriptions(conn, now)` returns ids, check one subscription per step (so a long channel doesn't starve downloads) with `subscriptions.check`. Needs a `ChannelLister` on the `Worker`/`JobRunner` side: reuse the `YtDlp` instance (`JobRunner.ytdlp`), injectable for tests (a `FakeChannelLister`).
- A flag `auto_check` (default True) like `auto_update` and `auto_purge`, so existing worker tests are unaffected; `__main__.py` unchanged apart from what's needed.
- Never let a check raise out of the loop (`check` already stores errors; also guard the call, log with `log.exception`).
- Skip the check while a yt-dlp update job is running if the existing code does the same for re-checks.
- Tests in `tests/test_worker.py` (or a new file): due subscription is checked once per step, a fresh check isn't repeated before the interval, "check now" request runs it, a paused one only on request, an exception in `check` doesn't stop the loop, `auto_check=False` does nothing.
- Report under 30 lines: what changed, tests, judgement calls.

## B: admin UI, sensor and i18n

- **Pages** (follow `tellybox/web/admin/` patterns: guard, Origin check, templates, `js_strings.py`, nav):
  - `/admin/subscriptions`: add form (channel URL, show picker, "include Shorts" toggle) using `YtDlp` through the same injectable seam the add page uses (see `tellybox/web/admin/add.py`; a pasted channel URL on the add form should now link to this page instead of the current "channel" message); after subscribing, a paged backlog (30 at a time, "load more") with checkboxes whose "approve selected" downloads those videos via `ingest.add(publish=True, show_id=...)` through the same path as `subscriptions.approve` would (the backlog items are not inbox items: use the same `ingest.add` call with the subscription's show; skip those already in the library); a list with last checked, last error, the warning badge (`unhealthy`), pause/resume, remove with confirm, "Check now" (one and all, through `subscriptions.request_check`); the global check interval (hours, min 1) on `/admin/settings`.
  - `/admin/inbox`: pending items newest first with a channel filter; cards with thumbnail, title, duration, channel, published date, "Open on YouTube" link and the translated warning; approve/reject per card; multi-select with bulk approve, bulk reject and "reject all remaining"; a "Rejected" tab with undo.
  - Nav: "Inbox" with the pending count badge, updated live through the admin SSE the dashboard already uses (see how the hub/dashboard pushes state; add the count to what is pushed or poll the same stream, whichever the existing code makes simple).
- **Sensor (HA-9):** `tellybox/web/api/state.py`: `AdminState.inbox = subscriptions.inbox_counts(...)` (from the DB, no cast service); `/api/info` capabilities gain `"inbox"`; the cold-start and "cast unreachable" shapes carry the inbox too; a change in the inbox must reach SSE listeners (the admin hub re-reduces on a refresh or push; make an inbox change trigger it within a couple of seconds). Update `docs/admin-api.md` only if the shape you build differs.
- **Check the forced-show side effect:** a queued source video now has `source_video.show_id` set before it is published. Make sure no library or disk-usage view treats that as "published in the show" (look at queries on `source_video.show_id` in `library.py` and the admin library pages), and add a test if a problem exists.
- **i18n (NF-13):** every string translatable (`_()`, templates, `t()` with `js_strings.py`); run `scripts/i18n.sh` and translate new entries in `tellybox/locale/{nl,de}` (informal tone, as the existing entries); `tests/test_i18n.py` must pass.
- **Tests:** page rendering, Origin check and admin guard on every POST, HTML escaping of titles and channel names, subscribe/pause/remove/check-now flows with the fake lister and fake cast, inbox approve/reject/bulk/undo, the badge count, the sensor in the state and `/api/info`, the sensor with the cast service down.
- Report under 40 lines: what changed, tests, judgement calls.
