# Step 10 (SponsorBlock): subagent briefs

Plan: `docs/plans/step10-sponsorblock.md`. Branch `step10/sponsorblock`. The contract is committed; build on it and don't change it without saying so in your report.

## Contract (done)

- **Migration `008_sponsorblock.sql`:**
  - `settings.sponsorblock_categories` (CSV, default `sponsor,selfpromo,interaction`, `''` = off);
  - `show.sponsorblock_categories` (NULL = global, `''` = off, CSV = own);
  - `source_video.sb_*` (`sb_categories`, `sb_segments_json`, `sb_removed_s`, `sb_status`, `sb_checked_at`, `sb_recheck_until`);
  - the `job` table rebuilt with the types `sb_recheck` and `redownload`;
  - the `position_shift` queue.
- **`tellybox/sponsorblock.py`** (pure): `CATEGORIES` (key → `N_()` label), `DEFAULT`, `RECHECK_DAYS`, `Segment`, `parse_csv`/`to_csv`, `effective_categories(conn, show_id)`, `segments_from_info`, `merge`, `removed_s`, `same_segments`, `to_original`/`from_original`/`remap_position`, and `dumps`/`loads` (the JSON used in `sb_segments_json` and `position_shift`).
- **`tellybox/ytdlp.py`:**
  - `download(..., sb_categories=[...])` → `DownloadResult.sponsor_segments`, where made-up chapters are dropped;
  - `sponsor_segments(url, categories)` for the re-check;
  - `SponsorBlockUnavailable(YtDlpError)`.
  - The fake yt-dlp (`tests/fixtures/ytdlp/fake_main.py`) knows the URL markers `sponsored`, `sbdown` and `nochapters`.
- **`tellybox/jobs.py`:** `JobType.SB_RECHECK` and `JobType.REDOWNLOAD` (claimed as `downloading`), `defer(conn, job_id, until, reason, now=)` (requeue without using an attempt), and `has_pending_for(conn, target_id, types)`.
- **`tellybox/ingest.py`:** `request_redownload(conn, source_id, *, with_sponsorblock, now)` (SB-4 admin action) and `RedownloadPending`.
- **`sb_status` meaning** for the current file:
  - `cut`: segments removed;
  - `none`: looked up, nothing in the enabled categories;
  - `unreachable`: downloaded uncut because the API was down (SB-5);
  - `off`: no categories enabled for the show;
  - `admin_off`: the admin chose "without SponsorBlock"; no cuts, no re-checks;
  - NULL: before v3, or just re-enabled by the admin.
- **`position_shift` semantics:** one row per episode whose file was replaced. `old_cuts_json` is what the old file had removed; `new_cuts_json` is what the new one has. Both are `sponsorblock.dumps` lists on the original timeline. The cast service applies `remap_position` to every `playback_position` row of that episode and deletes the row, in one transaction.

Rules for everyone: follow CLAUDE.md, including NF-13. Every interface string is translatable; run `scripts/i18n.sh` and translate nl and de. `tests/test_i18n.py` must pass. Match the surrounding code style. Run the whole suite (`.venv/bin/pytest -q`) before reporting. Don't commit; report the files changed and anything open.

## A: worker (`tellybox/ingest.py`, `tellybox/worker/`)

1. **`JobRunner.now_playing: Callable[[], int | None] | None = None`.** It returns the episode id loaded on the TV (`now_playing.episode_id` from the cast service's `GET /state`, whatever its state), or None. It raises when the cast service can't be reached. In `worker/__main__.py`, wire it with a small sync `httpx.get(f"http://{config.cast_api_host}:{config.cast_api_port}/state", timeout=5)`. In tests, `None` means "don't check".
2. **`_run_download`:**
   - Get `cats = sponsorblock.effective_categories(conn, show_id)`, where `show_id` is the show `find_show_by_channel` would pick, else None.
   - Call `download(..., sb_categories=cats or None)`. On `SponsorBlockUnavailable`, download again at once without SponsorBlock, with status `unreachable`.
   - `_publish` stores `sb_categories` (`to_csv(cats)` when the lookup happened, else NULL), `sb_segments_json`, `sb_removed_s`, `sb_status`, `sb_checked_at` (NULL when unreachable) and `sb_recheck_until = now + RECHECK_DAYS` (for every status).
   - SponsorBlock never fails a download (SB-5).
3. **`sb_recheck` job** (`_run_sb_recheck`):
   - It completes without doing anything when:
     - the source isn't `ready`;
     - `sb_status = 'admin_off'`;
     - the window has passed;
     - any episode of the source has `start_s` set (split, SB-6);
     - a redownload is pending.
   - Otherwise it takes the desired cuts: `[]` if `cats` is empty, else `ytdlp.sponsor_segments(url, cats)`.
     - On `SponsorBlockUnavailable`, it completes and leaves the row as it is; tomorrow catches up.
     - On another `YtDlpError`, `jobs.fail` as usual.
   - When the cuts differ from `loads(sb_segments_json)` (by `same_segments`), it enqueues `REDOWNLOAD`. Otherwise it sets `sb_checked_at`, turns `unreachable` into `none` when it found nothing, and updates `sb_categories`/`sb_status` if only the categories changed (for example `off`).
4. **`redownload` job** (`_run_redownload`):
   - The categories are `[]` when `admin_off`, else the effective ones.
   - **Before the download and again right before the swap:** if `now_playing()` returns one of this source's episode ids, or raises, `jobs.defer(..., now + 20 min, reason)` and return. Clean up tmp.
   - Then the same download → probe → convert → verify path, sharing code with `_run_download` rather than copying it. The `SponsorBlockUnavailable` fallback applies here too: it keeps the old file and fails retryably.
   - In one transaction:
     - `os.replace` onto the **same** `file_path`, with `FILE_MODE`;
     - update `episode.duration_s` for the source's episodes (`start_s IS NULL`);
     - insert one `position_shift` row per such episode when the cuts changed;
     - update the `sb_*` columns, keeping `sb_recheck_until`;
     - complete the job.
   - Don't touch the thumbnail, title, hidden, show or sort order, and don't change `source_video.status` (the episode stays playable). A failure leaves the published file and rows as they were.
5. **Scheduling (`worker/__init__.py`):** once per 03:00 slot day, the same pattern as `_purge_due` (in-memory day plus the `has_pending_for` guard), enqueue `SB_RECHECK` (`max_attempts=2`) for each source with `status='ready'`, `sb_recheck_until > now` and `COALESCE(sb_status, '') != 'admin_off'`. It also runs at startup.
6. **Tests** (`tests/test_ingest.py`, `tests/test_worker.py`, `tests/test_db.py`):
   - extend `FakeYtDlp` (segments per URL, `sponsor_segments`, `unavailable` switch);
   - a download with segments, none, off and unreachable;
   - recheck unchanged, changed, off, unreachable→none, admin_off skipped, window passed;
   - redownload: replaced file, same episode id, hidden, thumbnail and path, new duration, a `position_shift` row;
   - deferred while playing and when the cast service is unreachable;
   - a failure keeps the old file;
   - scheduling once a day;
   - migration 008 on a DB at version 7 with job rows keeps them.

## B: cast service (`tellybox/store.py`, `tellybox/cast/controller.py`)

1. **`store.apply_position_shifts(conn, now) -> int`** (the rows applied):
   - for each `position_shift` row in id order, and each `playback_position` of that episode: `position_s = remap_position(position_s, old, new)`, clamped to at most the episode's current `duration_s` when known;
   - keep `finished` and `updated_at` (so continue watching keeps its order; changed from `updated_at = now` in review);
   - delete the row, all in one `BEGIN IMMEDIATE`. It's cheap when the queue is empty (`SELECT 1 ... LIMIT 1` first).
2. **Call it** from the controller's `tick()` and in `play` before the resume position is read (`group_position`). Log how many were applied.
3. **Tests:** the store function (several profiles, a position inside a new cut, a cut undone, several queued shifts applied in order, other episodes untouched), and a controller test with the fake clock and fake Chromecast. A shift queued before `play` resumes at the remapped position.

## C: admin UI (`tellybox/web/admin/`, `tellybox/library.py`)

1. **Settings** (`settings_page.py`, template):
   - a "SponsorBlock" fieldset with one checkbox per `sponsorblock.CATEGORIES` (translated label), stored with `to_csv`;
   - no boxes ticked = off;
   - a short hint that segments are cut from new downloads and from videos still inside their 7-day re-check.
2. **Show page:** "SponsorBlock: use the default (lists them) / off / choose", with checkboxes shown for "choose". `library.set_show_sponsorblock(conn, show_id, categories: list[str] | None)` (None = default, `[]` = off). Add a `sponsorblock_categories` field to `library.Show` if it's needed.
3. **Episode page `GET /admin/episodes/{id}`,** linked from each episode row on the show page. It shows:
   - the title, show, duration and thumbnail;
   - the SponsorBlock section (SB-4): the table of removed segments (translated category label, original start–end as m:ss), the total removed, and the status line for each `sb_status`: "checked daily until <date>", "SponsorBlock couldn't be reached; it will be checked again", "off for this show", "downloaded again without SponsorBlock", "no segments found", or nothing for pre-v3 videos;
   - the buttons `POST /admin/episodes/{id}/redownload` with `sponsorblock=0|1`: "Download again without SponsorBlock" (with a confirm) when the file is cut or its status allows it, and "Download again with SponsorBlock" when it's `admin_off` or pre-v3;
   - a pending redownload shows "being downloaded again" and no buttons.

   Use `ingest.request_redownload`; `RedownloadPending` and `KeyError` show as messages. An episode without a source video (a manual upload) shows no SponsorBlock section. Follow the existing page and form patterns (CSRF, flash messages, `AdminContext`).
4. **Jobs page and dashboard:** labels for `sb_recheck` ("Check SponsorBlock") and `redownload` ("Download again") in `_labels.html`'s `job_type` and `jobs_page.py`, and the video title for those rows as for downloads.
5. **Tests:** in `tests/web/`, following the existing admin test patterns: settings save and load, the show override, the episode page for each status, the redownload POST (job queued, `admin_off`), and the pending refusal.
