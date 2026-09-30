# Step 10: SponsorBlock (v3), SB-1..SB-5

## Context
Next in the build order after the receiver (step 13, which is blocked on the owner's Cast Console registration). Sponsor, self-promotion and interaction segments must be cut out of the downloaded file, not skipped during playback (A-10). Per the CLAUDE.md stack rule we use yt-dlp's built-in support; there is no SponsorBlock client of our own. Branch `step10/sponsorblock` from `main`; the plan is committed as `docs/plans/step10-sponsorblock.md`.

## What the yt-dlp source (2026.08.19) tells us
- `--sponsorblock-remove CATS` runs `SponsorBlockPP` (`when='after_filter'`, **before** the download), then `ModifyChaptersPP`.
- The lookup sends only `sha256(id)[:4]` (`/api/skipSegments/<prefix>`), so SB-1's privacy requirement holds as is. A 404 means no segments.
- Chapters are shifted by ModifyChapters, so the `TBINFO` chapters already match the cut file (SB-1 chapters). `info["sponsorblock_chapters"]` holds the segments on the original timeline: category, start and end, with `type == "skip"`.
- **An unreachable API fails the whole run** with a `PostProcessingError` ("Unable to communicate with SponsorBlock API"). The lookup comes before the download, so this failure is cheap to detect and retry.
- Cuts use the concat demuxer with stream copy, so they land on keyframes. **Decision (owner, 2026-09-30): keyframe cuts, no `--force-keyframes-at-cuts`.**
- `--simulate --sponsorblock-mark CATS --print "%(sponsorblock_chapters)j"` fetches the segments without downloading, because the after_filter PPs run before the simulate check. The daily re-check uses this.

## Design

### Data (migration `008_sponsorblock.sql`)
- `settings.sponsorblock_categories TEXT NOT NULL DEFAULT 'sponsor,selfpromo,interaction'` (SB-2).
- `show.sponsorblock_categories TEXT`: NULL = the global setting, `''` = off, otherwise a CSV list.
- `source_video` gains:
  - `sb_categories`: categories applied to the current file; NULL = uncut.
  - `sb_segments_json`: the removed segments `[{category, start_s, end_s}]`, original timeline, merged.
  - `sb_removed_s`.
  - `sb_status`: `cut` | `none` | `unreachable` | `off` | `admin_off`.
  - `sb_checked_at` and `sb_recheck_until`.
- Rebuild the `job` table so its `type` CHECK allows `sb_recheck` and `redownload`. Nothing references `job`, so the rebuild fits in the migration transaction.
- New `position_shift (id, episode_id, old_cuts_json, new_cuts_json, created_at)`: a queue written by the worker and consumed by the cast service. The cast service stays the only writer of `playback_position` (CLAUDE.md).

### New `tellybox/sponsorblock.py` (pure, well tested)
- `CATEGORIES` (the yt-dlp keys with `N_()` labels), `DEFAULT`, `parse_csv`/`to_csv`.
- `effective_categories(conn, show_id)`: show override → global; empty = off.
- `segments_from_info(info, categories)`: the skip segments, filtered, sorted and merged.
- `same_segments(a, b, tol=0.5)`.
- `remap_position(pos, old_cuts, new_cuts)`: old file time → original timeline → new file time. A position inside a newly removed segment clamps to the segment's start (SB-3).

### yt-dlp wrapper (`tellybox/ytdlp.py`)
- `download(..., sponsorblock: list[str] | None)` adds `--sponsorblock-remove <csv>`. `DownloadResult` gains `sponsor_segments`, parsed from `TBINFO`.
- New `SponsorBlockUnavailable(YtDlpError)`, detected from stderr ("Unable to communicate with SponsorBlock API").
- New `sponsor_segments(url, categories) -> list[Segment]` (the simulate call above). It raises `SponsorBlockUnavailable` or `YtDlpError`.
- Extend `tests/fixtures/ytdlp/fake_main.py`: it echoes the sponsorblock args, emits `sponsorblock_chapters`, and has a mode that simulates the API being unreachable.

### Download (`tellybox/ingest.py`, `JobRunner`)
- `_run_download` looks up the effective categories. With `SponsorBlockUnavailable` it retries at once without SponsorBlock and sets `sb_status='unreachable'` (SB-5: SponsorBlock never fails a download).
- `_publish` stores the `sb_*` columns, with `sb_recheck_until = now + 7 d`.
- New `_run_redownload(job)` for the `redownload` type:
  - It runs the same download → convert → verify pipeline into tmp.
  - It **defers while the episode is loaded on the TV**: it asks the cast service's `/state` over localhost, and when that episode is playing or the cast service is unreachable, it re-queues with `run_after` +20 min instead of failing.
  - Then, in one transaction, it `os.replace`s the file at the same path, updates `episode.duration_s` and the `sb_*` columns, and inserts a `position_shift` row (NF-8).
  - The episode keeps its ID, title, thumbnail, show and hidden state. The source status stays `ready`, so the episode stays playable throughout.
- The job payload (a new nullable `job.options_json` column, or a flag) says whether SponsorBlock is used. "Download again without SponsorBlock" sets `sb_status='admin_off'`, which also stops the re-checks.

### Re-check (SB-3)
- In `tellybox/worker/__init__.py`, next to `update_due`: once a day in the 03:00 slot (same pattern), it enqueues one `sb_recheck` job per source with `sb_recheck_until > now`, `status='ready'`, `sb_status != 'admin_off'`, and no pending job.
- `sb_recheck` fetches the segments for the current effective categories. It compares them with `sb_segments_json` and queues a `redownload` if they differ, including categories turned off or changed (SB-2). When the API is unreachable, it just waits for the next day.
- A source still `unreachable` is always compared, so it catches up (SB-5).
- SB-6 (stop once split) belongs to step 12, but `sb_recheck` already skips sources whose episodes have `start_s` set.

### Cast service
- `tellybox/store.py`: `apply_position_shifts(conn)` rewrites the positions with `remap_position` and deletes the applied rows in one transaction.
- `tellybox/cast/controller.py`: it's called from `tick()` and before loading an episode in `play`.
- Timer tests use the fake clock and fake Chromecast.

### Admin UI (with nl/de, per NF-13)
- **Settings:** the global category checkboxes, with the default three pre-ticked (`settings_page.py` and its template).
- **Show page:** "SponsorBlock: use default / off / custom", with checkboxes (`library_pages.py`, `library.py`).
- **New episode page** `/admin/episodes/{id}` (decision, owner, 2026-09-30), linked from the show page's episode rows:
  - title, show, duration and thumbnail;
  - the removed segments (category label, original start and end) and the total time removed (SB-4);
  - the re-check status: "checked daily until …", "SponsorBlock was unreachable", or off;
  - buttons for "Download again without SponsorBlock" (confirm) and "Download again with SponsorBlock" (to undo `admin_off`);
  - the jobs page shows the new job types.
- Run `scripts/i18n.sh`, and translate `tellybox/locale/{nl,de}`.

### Docs
- `docs/PROGRESS.md` (step 10 entry).
- PRD data-model row: show categories NULL = global, `''` = off.
- `docs/cast-api.md` if the state changes; it doesn't need to, because the shift goes through the DB queue.
- The admin guide or README section on SponsorBlock.

## Execution
As in steps 8 and 9, a contract commit first, then (optionally) parallel subagents:
- **Contract:** migration 008, the `sponsorblock.py` signatures and tests, the ytdlp wrapper and fake, and the `position_shift` semantics.
- **A (worker):** download, fallback, redownload, recheck scheduling.
- **B (cast):** `apply_position_shifts`.
- **C (admin):** Settings, the show page and the episode page, with translations.
Run a cheaper model for the subagents for implementation work.

## Verification
- `pytest` (the whole suite, including `tests/test_i18n.py`). New tests:
  - `remap_position` and merge edge cases;
  - the download with segments, and with the API unreachable (uncut, `unreachable`, no failure);
  - recheck unchanged, changed and off → redownload;
  - redownload deferred while playing;
  - the replacement keeps episode id, hidden and thumbnail;
  - positions shifted by the cast service;
  - the migration on a v7 DB with existing jobs.
- Smoke test on a dev box: a real `yt-dlp` download of a known video with sponsor segments. Check `ffprobe` for the duration drop against `sb_removed_s`, and the episode page's list.
- Real-device checks (owner): play a cut episode on the TV and confirm the join is clean. Resume a partly watched episode after a forced redownload, and check the position moved correctly.
