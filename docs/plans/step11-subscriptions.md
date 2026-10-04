# Step 11: Channel subscriptions (v4, CS-1..CS-9, HA-9)

Status: proposed, spike done, waiting for the owner's approval to start part 0. Branch: `step11-subscriptions`.

A parent subscribes to a YouTube channel. The server checks it on a schedule and puts new uploads in an approval inbox. Nothing is downloaded or shown to the kids until the admin approves it (A-9). A Home Assistant sensor reports how many items wait.

## Decisions (owner, 2026-10-04, from a grilling session)

- **Scope:** a subscription watches the channel's Videos tab. A per-subscription "include Shorts" toggle is off by default. Livestreams count once they have finished (CS-1, A-31).
- **Backlog on subscribe:** existing uploads are listed paged, 30 at a time with "load more", for selection. Unselected ones never become inbox items. The subscribe moment is a baseline: only later uploads are "new" (CS-1, A-32).
- **One decision:** approving in the inbox means download, then publish when ready. There is no second "held" step, as for playlists. A-22 still keeps an auto-detected compilation hidden (CS-3, A-33).
- **Inbox states:** pending, approved, rejected. No snooze, no expiry. Rejections are remembered by YouTube ID and can be undone from a "Rejected" tab. Bulk select, bulk reject, and a pending-count badge in the admin nav (CS-6, A-34).
- **Show:** the channel's show by default (CI-4), or an existing show chosen at subscribe time. The subscription keeps a show reference, so renames and merges are followed (CS-9).
- **No rules:** no duration or title filters. A "long video" hint on a card is allowed; nothing is hidden or rejected automatically.
- **Schedule:** one global interval in the settings, default 6 h, minimum 1 h. "Check now" per subscription and for all. The worker runs it. A check fetches the newest 30 entries flat and stops at the first seen ID (see the spike result). If all 30 are new it pages on, to a cap of 100 and a logged warning (CS-2).
- **Failures:** each row shows "last checked" and the last error. Retries go on forever on the normal schedule. After 7 days of consecutive failures the row shows a warning badge. No push alerts. A failed check never touches existing content (CS-7, A-35).
- **Candidates:** already in the library, pending or rejected: skipped silently, keyed on YouTube ID. An upcoming premiere or live stream makes no item until it is a normal video. Members-only and age-restricted videos get an item marked "may not be downloadable"; the download job fails visibly and can be retried (CI-3). Same-title re-uploads are not detected (CS-5).
- **Pause and remove:**
  - Pause keeps the inbox visible and actionable. Resume picks up uploads made during the pause.
  - Remove deletes the pending items and keeps the rejection records. Re-subscribing gets a fresh baseline. The show and its episodes stay (CS-4, A-6).
- **UI:** "Inbox" and "Subscriptions" are separate top-level admin pages. The inbox is newest first with a channel filter. Each card shows thumbnail, title, duration, channel, published date and an "Open on YouTube" link. No embedded player (CS-8).
- **Home Assistant:** `AdminState` gets `inbox: {pending, unhealthy, latest_received_at}` and `/api/info` gets the `"inbox"` capability. It is read-only. `web` fills it from the database, and the admin hub pushes a change over SSE. HA-8 is about playback and overrides and does not apply to it (HA-9, A-36).

These become PRD requirements CS-1..CS-9 and HA-9 and assumptions A-31..A-36 in part 0.

## What already exists

- **Channel URLs:** `ytdlp.classify_url` already returns `"channel"` for `/@name`, `/channel/`, `/c/` and `/user/` URLs, and the add form points them at subscriptions (CI-7). Nothing consumes that yet.
- **Adding a video:** `ingest.add(conn, info, publish=…)` records a `source_video` and queues a download job. `publish=True` shows the episode when it is ready. `ingest.existing_youtube_ids` does the "already in library" check for playlists.
- **Shows by channel:** `show.youtube_channel_id` and `source_video.channel_id` link a video to its channel's show. A forced show needs a small addition (see part A).
- **Playlist listing:** `ytdlp.parse_playlist`, `PlaylistEntry` and `PLAYLIST_CAP` do flat listings. A channel listing is a variant with paging and a date cut-off.
- **Worker:** `tellybox/worker` runs the job loop and the daily yt-dlp update at 03:00. The periodic check goes next to it.
- **Hub and state:** `tellybox/web/api/state.py` builds `AdminState`, including `jobs.held_ready` (a count from `library.count_held_ready`). The admin hub caches jobs and disk for 10 s.
- **Migrations:** the last one is `016_watch_session_target.sql`, so this step takes `017_subscriptions.sql` and `018_subscription_check_request.sql`.
- **The PRD already lists** `Subscription` and `InboxItem` entities; part 0 fills them in.

## Spike result (yt-dlp 2026.08.19, 2026-10-04, three public kids' channels, nothing downloaded)

| Question | Finding |
| --- | --- |
| Flat listing of `/videos`, 30 entries | About 1 s. Entries have `id`, `url`, `title`, `duration` (always), thumbnails and `view_count`. They have **no** `upload_date`, `timestamp` or `channel_id`; `live_status` and `availability` are null on normal videos. |
| Dates | `--extractor-args "youtubetab:approximate_date"` adds a `timestamp` for free, but it is midnight UTC, can be a day off and ties between videos. A per-video call (about 3.4 s) gives the exact date, `live_status`, `availability` and `channel_id`. |
| Shorts | They are not in `/videos`. The `/shorts` tab is a separate listing: `/shorts/<id>` URLs, no duration, no date. |
| Live and upcoming | The `/streams` tab shows `live_status` (`is_live` seen; `is_upcoming` and `was_live` not seen, none were available). Entries that are live have no duration. |
| Paging | `--playlist-items 31:60` works, about 1.5 s a page; it re-walks from the start. |
| Channel ID | The listing's top level has the stable `channel_id` (UC…), the display name and the handle. Entries don't. |
| Errors | A bad handle takes three 404 retries and then exits 1 with `HTTP Error 404`; a bad UC id says "This channel does not exist". A terminated channel was not tested. |

Consequences for the design:
- **The baseline is a set of seen video IDs, not a date.** At subscribe time the newest 100 IDs (`/videos`, plus `/shorts` when the toggle is on) are stored in `subscription_seen`. A check lists the first page, treats unseen IDs as new, and pages on while all of a page is unseen (the cap of 100 stays). `baseline_at` is kept only for display. This replaces "stop at the baseline date" in the decisions above.
- **Dates are for display only.** The listing uses `approximate_date` for sorting and the "published" date on a card. For a candidate that becomes an inbox item, one metadata call (about 3.4 s, only for new IDs) gives the exact date.
- **A candidate is checked before it is queued:** one metadata call must show `live_status == "not_live"` and `availability == "public"`. Live and upcoming candidates are not marked seen, so a later check picks them up once they are normal videos (CS-5). Members-only and age-restricted videos still get their "may not be downloadable" item.
- **Shorts need a second listing:** with the toggle on, a check also lists `/shorts` and treats it the same way. A Short is recognised by its `/shorts/` URL, never by duration (Bluey has 3 s clips in `/videos`).
- **Errors:**
  - The channel URL is resolved to its `channel_id` at subscribe time and that is the key; the handle is for display.
  - A bad handle at subscribe time is rejected with a clear message, with the 404 retry delay capped.
  - A channel that disappears later is an ordinary failing subscription (A-35); the 7-day warning applies. The spike did not recommend a different rule and the owner's decision stands.
- **Still open:** the exact error text for a terminated channel; the implementation will test with a real example if one turns up and otherwise treat any non-zero exit as a failed check.

## Part 0: contract (controller, before the subagents)

1. **PRD:** CS-1..CS-9 (replacing the four rows), HA-9, the `Subscription` and `InboxItem` entities, A-31..A-36, the build-order text and the header line. Done in the planning commit, together with item 5.
2. **Migration `017_subscriptions.sql`:**
   - `subscription(id, channel_id UNIQUE, channel_name, channel_url, show_id NULL REFERENCES show ON DELETE SET NULL, include_shorts, paused, baseline_at, last_checked_at, last_ok_at, last_error, failing_since, created_at)`;
   - `inbox_item(id, subscription_id NULL REFERENCES subscription ON DELETE SET NULL, youtube_id UNIQUE, url, title, channel_name, duration_s, thumbnail_url, published_at, status CHECK (pending|approved|rejected), warning, received_at, decided_at)`: rejected rows outlive their subscription;
   - the baseline: `subscription_seen(subscription_id, youtube_id)`, filled at subscribe time with the newest 100 IDs and extended as checks see more; `baseline_at` is for display only;
   - `settings.subscription_check_hours INTEGER NOT NULL DEFAULT 6`;
   - `source_video.show_id NULL`: a forced show, which the publish step prefers over the channel lookup.
3. **`tellybox/subscriptions.py`**, written in full with tests, because parts A and B both use it: create, list, pause, resume, remove; `record_candidates`, `approve`, `reject`, `undo_reject`; `inbox_counts` (pending, unhealthy, latest_received_at); the dedupe rules of CS-5. It takes a `ChannelLister` interface, so tests use a fake listing.
4. **`ChannelLister` and the yt-dlp implementation:** `list_channel(url, tab, offset, limit)` for the `videos` and `shorts` tabs, returning entries with ID, title, duration, thumbnail, approximate date and the top-level `channel_id`; plus `video_status(id)`, the one-video metadata call for the live and availability check and the exact date. A `FakeChannelLister` for tests.
5. **Docs:** `docs/admin-api.md` (the `inbox` object, the `"inbox"` capability, the HA-8 note) and `docs/PROGRESS.md` (step 11 planned). Done in the planning commit.

## Part A: worker and ingest (subagent A)

- **Check job:** a periodic job in the worker. It reads `subscription_check_hours`, checks every unpaused subscription, and also runs when "Check now" enqueues it. It lists the newest 30, pages to 100 as above, passes the entries through `record_candidates`, and updates `last_checked_at`, `last_ok_at`, `last_error` and `failing_since`.
- **Approve:** `subscriptions.approve` calls `ingest.add(publish=True)` with the forced show, in one transaction with the status change. A candidate already in `source_video` is refused cleanly.
- **Forced show:** the publish step uses `source_video.show_id` when set; otherwise it keeps the channel lookup.
- **Errors:** a failed check is logged and stored, never raised out of the loop; a channel that no longer exists is just a failing subscription.

## Part B: admin UI, API and sensor (subagent B)

- **Subscriptions page (`/admin/subscriptions`):** the add form (a channel URL, a show picker, the Shorts toggle), the paged backlog with checkboxes ("approve selected" goes straight to the inbox's approve path), a list with last checked and last error, the warning badge, pause and resume, remove with a confirm, "Check now", and the global interval in the settings page.
- **Inbox page (`/admin/inbox`):** pending items newest first with a channel filter; cards as in CS-8; approve and reject; multi-select with bulk approve and bulk reject, and "reject all remaining"; a "Rejected" tab with undo.
- **Nav badge:** the pending count, updated through the existing SSE hub.
- **Sensor:** `build_admin_state` gains `inbox` from `subscriptions.inbox_counts`; `/api/info` lists `"inbox"`. The count is refreshed on change and does not need the cast service.
- **Add form:** a pasted channel URL goes to the subscribe form instead of an error.
- **i18n:** every new string through the usual flow, then `scripts/i18n.sh`, with nl and de translated.

## Execution (at most 2 subagents)

- The controller writes and commits the spike result and part 0 on `step11-subscriptions`.
- A and B run in parallel on Sonnet, each in its own worktree off the contract commit. The briefs go in `docs/plans/step11-handoff.md`.
- The controller merges A then B, runs the full suite and `scripts/i18n.sh`, then reviews:
  - nothing downloads or publishes without an approve (A-9), including through paging, "Check now" and resume;
  - a rejected ID never comes back, also after remove and re-subscribe;
  - every inbox route needs the admin session and the Origin check;
  - titles and channel names are escaped in the inbox and subscriptions pages;
  - the sensor works with the cast service down.
- Then a manual smoke test with a fake lister, and the PR.

## Tests (fake yt-dlp channel listing, fake clock)

- **Baseline:** uploads before the baseline never reach the inbox; ones after do; the unselected backlog items stay out.
- **Dedupe:** already in library, already pending, already rejected, same ID from two subscriptions.
- **Candidates:** an upcoming premiere makes no item and appears once it is a normal video; a members-only video makes an item with a warning; Shorts follow the toggle.
- **Paging:** 30 entries with a known ID stops early; 30 all new pages on; the cap of 100 logs a warning.
- **Decisions:** approve queues a download with `publish`, in the right show; reject, undo, bulk; an approved item cannot be approved again.
- **Pause, resume, remove, re-subscribe:** uploads during a pause are found on resume; remove deletes pending items and keeps rejections; a fresh baseline after re-subscribing; the show and episodes survive.
- **Failures:** the error is stored, the next check retries, `failing_since` clears on success, the 7-day warning flag, existing content untouched.
- **Schedule:** the interval and its 1 h minimum; "Check now".
- **Sensor:** `pending`, `unhealthy` and `latest_received_at` change as items arrive and are decided; the `"inbox"` capability; the state still answers when the cast service is down.
- **Admin:** the pages, the Origin check, escaping, the badge; `tests/test_i18n.py` passes.
- The existing tests keep passing.

## Real-device checks (owner, after deploy)

1. On the phone, open Subscriptions, paste a kids' channel URL, pick a show, pick two backlog videos and approve them. They download and appear in the kid app; one plays on the Chromecast.
2. Pick a channel that posts often. After the next check (or "Check now") a new upload shows in the Inbox with its badge. If the channel is quiet, move the baseline back to simulate one.
3. Reject one item, find it under "Rejected", undo it, then approve it.
4. Pause the subscription, check, resume, and see the uploads from the pause. Remove it and confirm the episodes stay.
5. With a read token, `GET /api/admin/state` shows `inbox.pending`, `inbox.unhealthy` and `inbox.latest_received_at`. In Home Assistant, an automation on `latest_received_at` fires when a new item arrives.

## Open details for the implementation

- Whether `source_video.show_id` or a different seam is the cleanest way to force a show; the publish step decides.
- The exact wording of the "may not be downloadable" and "long video" hints. English text is the message id (NF-13).
