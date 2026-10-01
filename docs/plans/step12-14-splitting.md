# Steps 12 + 14: Episode splitting (v5 manual, v6 smart)

## Context
Long YouTube compilations should become separate episodes with little manual effort (PRD goal 4). The owner chose to build splitting now, both manual (step 12, v5: ES-1, ES-2, ES-7, ES-8, SB-6) and smart (step 14, v6: ES-3..ES-6, ES-9, ES-10). Step 11 (subscriptions) waits. Decision (owner, 2026-10-01): the approve form has a "delete the source file after cutting" checkbox, unticked by default. This closes the PRD open question; it becomes A-21.

Per CLAUDE.md there is one step per branch: `step12/manual-split` first (merged, deployed, device-checked), then `step14/smart-split` built on it. Both are built the same way as steps 8–10. The controller (me) commits a contract, then **three Sonnet subagents** work in parallel worktrees on disjoint files. The controller merges, reviews and smoke-tests. This plan is committed as `docs/plans/step12-14-splitting.md`, and the briefs as `docs/plans/step12-handoff.md` and `step14-handoff.md`.

## What the code already gives us
- `episode.start_s`/`end_s` exist, but they are NULL everywhere. `start_s IS NOT NULL` is already the "split" marker. `_run_sb_recheck` and `_run_redownload` skip split sources (`ingest.py:486`).
- `source_video.chapters_json` holds `[{start_s,end_s,title}]`. **Bug found:** `_publish` keeps the add-time chapters through `COALESCE(chapters_json, ?)` (`ingest.py:425`). Those are on the *original* timeline, while yt-dlp's download chapters are already shifted for the SponsorBlock cuts.
- `/media/{episode_id}/…` serves `episode.file_path` whole and ignores offsets, so split episodes must be separate files (ES-8 anyway).
- Reusable pieces:
  - `media_format._ffmpeg_cmd`, `_run_ffmpeg` (progress), `probe` and `verify`;
  - `images.grab_frame` (`-ss` before `-i` with decode is frame-accurate);
  - `images.save_episode_thumbnail`;
  - `library._remove_unreferenced`/`_FILE_REFS`;
  - the redownload "defer while on the TV" pattern (`JobRunner.now_playing` + `jobs.defer`);
  - `_swap`-style one-transaction publishing;
  - `_parse_time`;
  - the `/admin/episodes/{id}/frame.jpg` route.
- Gaps:
  - there is no admin route that streams a source MP4;
  - `list_held_downloads` assumes one episode per source (a LEFT JOIN gives duplicate rows);
  - the job CHECK needs a rebuild for new types;
  - `_enqueue_rechecks` still queues no-op rechecks for split sources.

---

# Step 12: Manual splitting (v5)

## Design

### Data: migration `009_splitting.sql`
- `split_proposal` has one row per source video (`source_video_id UNIQUE`, CASCADE):
  - `status`: `draft` | `review` | `approved` | `cutting` | `done` | `failed`;
  - `origin`: `chapters` | `manual` | `detected`;
  - `segments_json`: `[{start_s, end_s, title, keep}]` on the file timeline. They are contiguous, and `keep=false` drops a part, such as a channel intro;
  - `delete_source`, `error`, `created_at`, `updated_at`, `approved_at`.
- The `job` table is rebuilt (the 008 pattern) with types `split` and `detect`. `detect` is added now, so step 14 needs no second rebuild. `target_id` stays `source_video.id`.

### New pure module `tellybox/splitting.py` (contract, well tested)
- `Segment` dataclass, `dumps`/`loads`.
- `segments_from_cuts(duration, cuts, titles)` and `cuts_of(segments)`.
- `validate(segments, duration)`: contiguous; each kept segment is at least 5 s; at least one segment is kept; there are at least 2 segments or 1 trimmed one.
- `chapters_on_file(source)`: chapters on the file timeline. If SponsorBlock cut the file and the chapters end past the file's duration, it maps them with `sponsorblock.from_original`. This fixes rows written before the `_publish` fix.
- `segments_from_chapters(chapters, duration)` (ES-1).
- `episode_file_name(youtube_id, job_id, n)` gives `<youtube_id>-s<job>-<nn>.mp4`, so a re-split never overwrites the parts it replaces.

### Cutting: `media_format.cut(src, dst, start_s, end_s, on_progress)`
- It uses `_ffmpeg_cmd` with `-ss start` before `-i` and `-t dur`, always re-encoding with the same x264 veryfast/AAC/faststart/≤720p settings and `nice`. That makes it frame-accurate (ES-8).
- It writes a `.part` file, then `verify`.

### Worker: `split` job (`ingest.JobRunner._run_split`)
1. **Defer** 20 minutes while any episode of the source is on the TV, or while the cast service can't be reached (as redownload does).
2. **Cut** each kept segment into `.tmp/job-<id>/`, with weighted progress. Take a thumbnail with `grab_frame` at `start + min(3 s, len/4)`. Verify everything.
3. **Publish** in one `BEGIN IMMEDIATE` transaction:
   - move the files to `shows/<show>/<ytid>-s<job>-<nn>.mp4`;
   - insert the episodes with `start_s`/`end_s` and the titles. They take the old episode's `sort_order` slot, and the episodes after them are renumbered. Hidden = the old episode's hidden state or a held source;
   - delete the source's previous episodes, which are the whole video or an earlier split. Their playback positions go with them by cascade, and history keeps its rows (SET NULL);
   - set the proposal to `done`;
   - with `delete_source`, set `source_video.file_path = NULL`.
4. **After the commit**, `_remove_unreferenced` deletes the old files. **On any failure:** roll back, unlink the moved files, mark the proposal `failed` with the error, and keep the old episode playable (NF-8).
- **Re-split:** allowed while the source file exists. The approval replaces the current split episodes.

### SB-6 and small fixes
- `_enqueue_rechecks` skips split sources.
- `request_redownload` raises `SourceSplit`. The episode page explains: "This video was split into episodes; SponsorBlock no longer re-checks it." This closes the step 10 open item.
- `_publish` and the redownload store the download's (shifted) chapters.
- `list_held_downloads` gives one row per source.

### Library API (contract signatures, implemented by A)
- `get_split(conn, source_id)`, which creates a draft from the chapters on first use (ES-1);
- `save_split(conn, source_id, segments, *, status)`;
- `approve_split(conn, source_id, delete_source, now)`, which validates, sets `approved` and enqueues `SPLIT`. It refuses while a split is pending or the source file is gone;
- `discard_split`;
- `list_splits_for_review(conn)`.

### Admin
- `GET /admin/sources/{id}/video.mp4`: the session-guarded `FileResponse` (with Range) for the scrub player.
- `GET /admin/sources/{id}/frame.jpg?t=`: an exact frame for the thumbnail strip.
- Split page `/admin/sources/{id}/split`, linked from the episode page ("Split into episodes" / "Split again"):
  - **Player (ES-2):**
    - a `<video>` with step buttons (−10 s, −1 s, −1 frame, +1 frame, +1 s, +10 s) and keyboard shortcuts;
    - "Cut here";
    - "Use chapters" (ES-1).
  - **Segment list:**
    - start–end;
    - a title input;
    - a keep toggle;
    - delete cut;
    - nudge ±1 s / ±0.1 s.
  - **Review strip (ES-7):** the start frame and timestamp at each cut. Tapping one seeks the player.
  - **Approve form:**
    - a "Delete the source file after cutting" checkbox, unticked;
    - the estimated encode time;
    - Discard.
  - Drafts autosave through `GET`/`PUT /admin/api/splits/{source_id}` (JSON, Origin-checked). Errors go to a flash or inline.
  - It's phone-first (NF-12) and works on a 4.7" screen.
- **Library page:** a "Splits to review" section (drafts in `review`, failed or cutting). The jobs page and dashboard get the `split` and `detect` labels.
- **Episode page of a split episode:** "Part n of <source>", with a link back to the split page.
- **i18n:** every string goes through `_()`/`t()`. Run `scripts/i18n.sh`, and add the nl and de translations.

### Docs
- `docs/PROGRESS.md`;
- PRD: A-21 (the delete checkbox), closing the open question, and the data-model rows;
- `docs/installation.md`: disk note (the source and the episodes both exist until the source is deleted);
- README feature line.

## Execution (step 12)
- **Contract (controller):**
  - migration 009 and its migration test;
  - `splitting.py` with tests;
  - `JobType.SPLIT`/`DETECT`;
  - the `media_format.cut` and `library` split signatures, stubbed;
  - the split JSON API shape and the DOM hooks of `split.html` (`data-*` attributes) written down in the handoff;
  - commit.
- **A: worker and library** (`media_format.py`, `ingest.py`, `worker/`, `library.py`):
  - `cut`, `_run_split`, the library split functions, the SB-6 and chapters fixes, the held-list fix;
  - tests with real ffmpeg on lavfi clips. These check the duration of each part, frame accuracy (a clip with a burned-in frame counter or colour change at known times), sort order, hidden, the failure rollback, defer while playing, delete-source, and re-split.
- **B: admin routes and templates** (`library_pages.py` or a new `split_pages.py`, templates, `_labels.html`, `jobs_page.py`, the catalogs):
  - the source video and frame routes, the split page shell, the JSON API, approve and discard, the episode-page links and SB-6 message, the review section, the job labels, and nl and de;
  - tests in `tests/web/admin/test_split_pages.py`.
- **C: split editor front end** (`admin/static/split.js`, `split.css`, entries in `js_strings.py`):
  - the player controls, cut marking, the segment list, nudges, the strip, autosave, and the phone layout;
  - it works against the contract's JSON shape and DOM hooks;
  - a pure-logic test where practical (cut list operations in a small module).
- Each agent: `model: sonnet`, `isolation: worktree`. Run the full suite, don't commit, and report the files changed and anything open. The controller merges, resolves the catalog conflicts (`scripts/i18n.sh` again), and reviews (`/code-review`).

## Verification (step 12)
- `.venv/bin/pytest -q`: the whole suite, including `test_i18n.py`.
- **Dev-box smoke test with real uvicorn and the worker:**
  - download a real chaptered compilation;
  - the split page offers its chapters;
  - adjust one cut, rename, drop the intro, and approve with "delete source";
  - `ffprobe` each part's duration against the segments; the episodes are in order, with thumbnails; the source file is gone.
- **Real-device checks (owner):**
  - play a split episode on the TV: clean start, no stray frames from the previous episode;
  - autoplay into the next part;
  - a split held back while the compilation was playing ran after it stopped;
  - the redownload button shows the SB-6 message.

---

# Step 14: Smart splitting (v6)

## Design
- **Decisions (owner, 2026-10-01):**
  - A-22 agreed: an auto-detected compilation stays hidden until its split is approved or it's published whole.
  - No PySceneDetect: version 0.7 requires the desktop OpenCV build. Scene changes come from ffmpeg `scdet`, and black frames from `blackdetect`.
  - Tesseract with English, Dutch and German goes into the image.
- **Dependencies:**
  - `opencv-python-headless`, `numpy`, `pillow`, `pytesseract`;
  - `tesseract-ocr` plus `eng`/`nld`/`deu` in the image (ES-9, optional at runtime: skipped when the binary is missing);
  - CI installs `tesseract-ocr` so the OCR tests run there.
  - Measure the image size growth.
- **Matching measured (contract):**
  - pHash proved unstable on mostly flat crops: an identical card scored 10–14 bits.
  - Using the same scaler on both sides also matters. Every frame, sampled or marked, goes through `detect.SAMPLE_FILTER` in ffmpeg.
  - With **dHash**, on the synthetic cards: the same card 0–1, a heavily degraded card 9–12, anything else 22 or more. So the default threshold is 6, and an ES-5 re-scan accepts threshold + 8.
- **Migration `010_split_profile.sql`:**
  - `split_profile (show_id PK, match_threshold, length_hint_s, snap_window_s DEFAULT 30, ocr INTEGER, auto_detect INTEGER, ocr_region_json, updated_at)`;
  - `split_reference (id, show_id, image_path, region_json, card_hash, source_video_id, at_s, created_at)`. The image paths go into `_FILE_REFS` (media `split/show-<id>-<ts>.jpg`).
  - `split_proposal.detected_json`: per-cut details for the review screen.
  - `source_video.awaiting_split` (A-22).
- **New package `tellybox/detect/`** (pure, tested with synthetic lavfi clips that have title cards inserted at known times):
  - `sample.py`: decode at about 2 fps through an ffmpeg pipe into small grayscale frames (ES-4). NF-6 says under half the video's length; measure it.
  - `match.py`: crop the region, compute the dHash, and compare its Hamming distance to the references. A run of matching frames becomes one hit at the run's start.
  - `hint.py` (ES-5):
    - drop hits closer than 0.5× the length hint;
    - when a gap is over 1.5× the hint, re-scan ±20 % around the expected boundary at 5 fps with a looser threshold.
  - `snap.py` (ES-6): ffmpeg `scdet` plus `blackdetect` in `[hit − window, hit]`. It takes the nearest change before the hit, or else the hit itself.
  - `ocr.py` (ES-9): Tesseract on the title-card frame (or the OCR region), cleaned up. An empty result means no title.
  - `detect(video, profile, on_progress) -> list[Cut]` with a confidence per cut.
- **Worker:**
  - The `detect` job writes a proposal (`origin='detected'`, `status='review'`). It never cuts by itself (ES-7 review stays mandatory).
  - ES-10: after a download, when the show's profile has `auto_detect` and the video is longer than 1.5× the length hint, it queues `detect`. **A-22 (agreed 2026-10-01):** such a compilation is published *hidden* until its split is approved or the admin publishes it whole, so a long unsplit video never appears by surprise.
- **Admin:**
  - Split page:
    - "Mark as title card" at the current frame (ES-3), with a region drawn over the still frame (canvas drag, normalised coordinates) and saved to the show's profile;
    - "Detect cuts" (queues `detect` and shows its progress);
    - the cuts it found show a confidence badge and the OCR titles prefilled.
  - Show page: a splitting-profile card with the references (thumbnails with their region outlined, delete), threshold, length hint, snap window, OCR and auto-detect.
  - Detected proposals appear in "Splits to review", and as a dashboard count.

## Execution (step 14)
- **Contract:** dependencies, migration 010, the `detect/` types and hashing (implemented) and engine signatures, the `library` profile functions (implemented), the API and DOM additions, and the synthetic test-clip fixture (`tests/detect_clips.py`).
- **A:** the `detect/` engine and its tests, plus a benchmark on a 30-minute synthetic clip against NF-6.
- **B:** worker integration (the detect job, ES-10 hold and auto-queue, A-22 in the split job).
- **C:** the admin routes and UI (marking with the region editor, the profile card, the Detect button, confidence badges, publish whole), with nl and de.
- Same rules: Sonnet, worktrees, the controller merges and reviews.

## Verification (step 14)
- Full `pytest`. The detection tests find the inserted title cards within ±1 s; the length hint recovers a weakened card; snapping lands on the inserted black frame.
- **Dev-box run** on two real shows: time the detection against the video length (NF-6), and compare the detected cuts with the chapters or a manual split.
- **Owner gate:** detection accepted on two shows (v6 gate); an auto-detected compilation waits hidden in review, then plays split on the TV.
