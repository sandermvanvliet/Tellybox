# Step 14 (smart splitting): subagent briefs

Plan: `docs/plans/step12-14-splitting.md` (the step 14 part). Branch `step14/smart-split`, built on step 12 (merged). The contract is committed. Build on it, and don't change it without saying so in your report.

## Contract (done)

- **Dependencies:**
  - `opencv-python-headless`, `numpy`, `pillow`, `pytesseract` in `pyproject.toml`;
  - the Dockerfile installs `tesseract-ocr` with `eng`, `nld` and `deu`;
  - CI installs `tesseract-ocr` and `eng`;
  - **no PySceneDetect** (owner, 2026-10-01): ffmpeg `scdet` and `blackdetect` do the scene snapping.
  - On the dev box the tesseract binary may be missing; OCR tests must skip then (`detect.ocr.available()`).
- **Migration `010_split_profile.sql`:**
  - `split_profile` (one per show: threshold, length hint, snap window, OCR and its region, auto-detect);
  - `split_reference` (`image_path`, `region_json`, `card_hash`, `source_video_id`, `at_s`);
  - `split_proposal.detected_json`;
  - `source_video.awaiting_split` (A-22).
- **`tellybox/detect/__init__.py`** (implemented):
  - `Region` (fractions 0..1, `crop`, `to_json`/`from_json`), `Reference(card_hash, region)`, `Profile`, `Hit`, `Cut`;
  - `SAMPLE_FPS = 2`, `SAMPLE_WIDTH = 320`, `SAMPLE_FILTER` (the one ffmpeg scaler for every frame);
  - `DEFAULT_THRESHOLD = 6`, `RESCAN_LOOSER = 8`;
  - `normalise_image(bytes)`, `hash_region(frame, region)` (a **dHash**; see the plan for the measurements), `reference_hash(image, region)`.
  - `detect(video, profile, on_progress=None) -> list[Cut]` is a stub.
- **Stubs for slice A:**
  - `sample.frames(video, fps=, start_s=, end_s=, on_progress=, nice=)`;
  - `match.hits(frames, references, threshold, max_gap_s=1.5)`;
  - `hint.apply(found, duration_s, length_hint_s, rescan)`, with `TOO_CLOSE`, `TOO_FAR` and `RESCAN_SPAN`;
  - `snap.snap(video, hit_s, window_s)`;
  - `ocr.available()` (implemented) and `ocr.read_title(frame, region)`.
- **`tellybox/library.py`** (implemented, tests in `tests/test_split_profile.py`):
  - `SplitProfile`/`SplitReference`, `get_split_profile` (defaults when nothing is saved), `save_split_profile` (ValueError on out-of-range values);
  - `add_split_reference(conn, media_dir, show_id, jpeg, region, *, source_video_id, at_s, now)`, which writes `split/show-<id>-<ts>.jpg` and stores the hash; `delete_split_reference`;
  - references are in `_FILE_REFS` and deleted with their show;
  - `request_detect(conn, source_id, *, now) -> job_id`, which raises KeyError, SourceGone, LookupError (no marked card) or SplitLocked;
  - `split_show_id`;
  - `save_detected_split(conn, source_id, segments, detected, *, now)` (status `review`, origin `detected`) and `get_detected(conn, source_id)`;
  - `publish_unsplit(conn, source_id, *, now)` (A-22: clears `awaiting_split` and unhides, unless the download is held);
  - `SplitListItem.awaiting_split`.
- **`tests/detect_clips.py`:** `make_compilation(out, episode_s, *, card_s=2, black_s=0.6, weak=set(), lead_s=3)`, which returns `card_starts`, `black_starts`, a `reference` JPEG and `titles` ("Episode n", drawn with a system font when `fc-match` finds one). Use `LOGO_REGION` as the region. A *weak* card (noise, darker, less contrast) is 9–12 bits from the reference, a normal one 0–1, and anything else 22 or more.

**Rules for everyone:**
- Follow CLAUDE.md, including NF-13: every interface string is translatable. Run `scripts/i18n.sh` and translate `tellybox/locale/{nl,de}` (je/du). `tests/test_i18n.py` must pass.
- Match the surrounding style.
- Run the whole suite before reporting. Your worktree has no `.venv`, so use `/home/sander/Projects/Tellybox/.venv/bin/pytest -q` from the worktree directory.
- First make sure the worktree is on the contract commit. Run `git log --oneline -3`; if it isn't, run `git reset --hard step14/smart-split`, since the worktree has no changes of its own yet.
- Don't commit. Report the files changed, any contract change and anything open.

---

## A: the detection engine (`tellybox/detect/sample.py`, `match.py`, `hint.py`, `snap.py`, `ocr.py`, `detect()` in `__init__.py`; tests `tests/test_detect.py`)

1. **`sample.frames`:**
   - one `nice`d ffmpeg process: `-ss start` before `-i` when start > 0, `-t` for the end, and `-vf fps=<fps>,` + `SAMPLE_FILTER`, writing `-f rawvideo -pix_fmt gray pipe:1`;
   - read exact frame-size chunks (height from `probe`: `round(h * 320 / w)`, made even as `-2` does) and yield `(start_s + i / fps, frame)`;
   - report progress as `i / (fps * span)`;
   - kill ffmpeg if the consumer stops early; raise `MediaError` with the stderr tail on failure.
2. **`match.hits`:**
   - a frame matches when the minimum over the references of `hash_region(frame, ref.region) - ref.hash()` is at most `threshold`;
   - consecutive matches, bridging gaps up to `max_gap_s`, form one `Hit(start_s, end_s, best distance, reference index)`.
3. **`hint.apply`:**
   - without a hint, return the hits sorted;
   - with a hint:
     - walk the hits in time order, dropping any closer than `TOO_CLOSE × hint` to the previous kept one (keep the better distance when they compete);
     - for each gap (including from 0 to the first hit, and from the last hit to the end) longer than `TOO_FAR × hint`, call `rescan(expected − RESCAN_SPAN × hint, expected + RESCAN_SPAN × hint)` at each expected boundary (previous + k·hint), and keep the best hit found;
     - hits found by a re-scan are marked so `detect` gives them lower confidence. `Hit` is frozen and its fields are the contract: add an optional field with a default if you need one, and say so.
4. **`snap.snap`:**
   - over `[max(0, hit − window), hit]`, run one ffmpeg pass `-ss … -t … -vf "blackdetect=d=0.1:pix_th=0.10,scdet=threshold=10" -f null -`;
   - parse `black_start` and `lavfi.scd.time` from stderr (with `-loglevel info`);
   - the nearest change before the hit wins. Prefer black over a scene change within 1 s, and return the black start (the cut lands at the start of the black, so the previous episode ends cleanly);
   - with none, return `(hit_s, None)`. Times are absolute.
5. **`ocr.read_title`:**
   - crop the region of a full-resolution gray frame, upscale ×2, Otsu threshold, and `pytesseract.image_to_string(..., lang=LANGUAGES, config="--psm 7")`;
   - keep letters, digits, spaces and basic punctuation, collapse whitespace, and return "" for fewer than 3 letters;
   - return "" when `not available()`.
6. **`detect.detect`:**
   - `found = match.hits(sample.frames(video), refs, threshold)`;
   - then `hint.apply(...)` with a `rescan` that samples the span at 5 fps with `threshold + RESCAN_LOOSER`;
   - drop a hit at or before 1 s (the video already starts there);
   - each hit becomes a `Cut`:
     - `at_s` = the snap of `hit.start_s`;
     - `confidence` = `1 − distance / (threshold + RESCAN_LOOSER + 1)`, halved for re-scan hits;
     - `title` from OCR when `profile.ocr`. Grab the frame at `hit.start_s + 0.5` at full resolution with `images.grab_frame` and decode it with `cv2.imdecode`. The region is `profile.ocr_region`, or the whole frame when it is None (changed after testing: titles rarely sit inside the matched logo);
   - progress: sampling is 80 %, the rest 20 %.
7. **Tests**, with `make_compilation` (session-scoped fixtures, small sizes):
   - `frames` count and times;
   - `hits` finds every normal card within ±0.5 s and nothing else;
   - the weak card is missed at the default threshold but found through the hint re-scan;
   - a false hit too close is dropped;
   - `snap` lands on `black_starts` within ±0.1 s;
   - `snap` without black or scene change in the window returns the hit;
   - full `detect` cuts within ±0.2 s of `black_starts`, in order, with confidences;
   - OCR reads "Episode 2" (skip without tesseract or without a font, i.e. `font_file()` is None).
   - **Benchmark (NF-6):** a 30-minute compilation at 1280×720, with `pytest.mark.slow` (register the marker in `pyproject.toml` and skip it by default with `-m "not slow"` in `addopts`). It asserts detection takes under half the duration. Run it once and report the time.

---

## B: worker (`tellybox/ingest.py`, `tellybox/worker/`; tests `tests/test_detect_job.py`, `tests/test_ingest.py`)

1. **`JobRunner._run_detect(job)`** replaces the step 12 "arrives in v6" failure:
   - load the source and its file. Missing or not ready fails as not retryable;
   - `profile = library.get_split_profile(conn, library.split_show_id(conn, source.id))`. Unusable fails as not retryable ("no title card marked for this show");
   - build `detect.Profile` from it (`Reference(card_hash, Region(*region) if region else None)`, `ocr_region`);
   - run `detect.detect(path, profile, on_progress=self._progress(job.id))`;
   - turn the cuts into segments with `splitting.segments_from_cuts(duration, [c.at_s …], titles)`. The title of segment i+1 is cut i's OCR title; the first segment's title is "".
   - drop cuts that would make a kept part shorter than `splitting.MIN_KEPT_S` (merge into the previous part);
   - `library.save_detected_split(...)` with one dict per kept cut, then `jobs.complete`.
   - Zero cuts still saves a review proposal (the whole video), so the admin sees "nothing found".
   - `SplitLocked` completes without saving (the admin approved something meanwhile).
   - `MediaError` is retryable; anything else fails as usual.
   - The detect job doesn't defer for playback, because it reads the file only.
2. **ES-10 in `_publish`:**
   - after a *new* download (not a redownload), when the show's profile is usable and has `auto_detect`, and the duration is over `1.5 × length_hint_s` (or, with no hint, over 20 minutes), the new episode is created **hidden** and the source gets `awaiting_split = 1` (A-22);
   - after the commit, `library.request_detect` (ignore LookupError and SplitLocked).
3. **A-22 in `_publish_split`:**
   - when the source is `awaiting_split`, the parts are visible (unless the source is held) and the flag is cleared in the same transaction;
   - the rule "hidden when every old episode was hidden" doesn't apply then, since the old episode was hidden only for the review.
4. **Tests:**
   - detect job → proposal (`review`, `detected`, cuts near `black_starts`, OCR titles when available, `detected_json`);
   - no reference fails;
   - zero cuts;
   - SplitLocked;
   - ES-10 auto: hidden, flag set, DETECT queued; not queued when short, when auto is off, or for a redownload;
   - approving the auto proposal and running the split makes the parts visible and clears the flag;
   - a held source's parts stay hidden.

   Use `tests/detect_clips.py` with small clips, and the `FakeYtDlp` pattern from `tests/test_ingest.py` for ES-10.

---

## C: admin (`tellybox/web/admin/split_pages.py`, `library_pages.py`, templates `split.html`, `show.html`, `library.html`, `dashboard` if cheap; static `split.js`, `split_plan.js` only if needed, new `split_mark.js`, `split.css`; `js_strings.py`; catalogs; tests `tests/web/admin/test_split_profile_pages.py`, node tests if you add pure JS)

1. **Split state additions** (the JSON from `GET /admin/api/splits/{id}` and `#split-state`):
   - `"show_id"`;
   - `"profile": {"usable": bool, "references": [{"id", "image_url", "region"}], "length_hint_s", "auto_detect"}`;
   - `"detected"`: the result of `library.get_detected`, or null;
   - `"awaiting_split"`: bool;
   - `"detect_job"`: the newest DETECT job while queued or running, else null.
2. **Routes:**
   - `POST /admin/api/splits/{id}/reference`, JSON `{"at_s": float, "region": [x, y, w, h] | null}`:
     - `images.grab_frame(source file, at_s)`, then `library.add_split_reference(...)`;
     - returns the new `profile` block; 422 on a bad region; 409 when the source file is gone.
   - `POST /admin/api/splits/{id}/detect`: `library.request_detect`, which returns `{"job_id"}`. LookupError gives 409 "Mark a title card first", SplitLocked 409, SourceGone 409.
   - `GET /admin/img/split-reference/{id}.jpg` (`no-cache`).
   - `POST /admin/shows/{id}/split-profile`, a form: threshold, length hint in minutes (mm or mm:ss via `_parse_time`), snap window in seconds, OCR, OCR region (optional four numbers), auto-detect. ValueError re-renders with 422.
   - `POST /admin/split-references/{id}/delete`, back to the show page.
   - `POST /admin/sources/{id}/publish-whole`: `library.publish_unsplit`, with a flash (A-22).
3. **Split page (ES-3, ES-4, ES-7):**
   - **"Mark as title card":** freezes the current frame into an overlay `<canvas>` over the video. The admin drags a rectangle (pointer events, so it works with touch) or chooses "Whole frame", then Save, which POSTs the reference. The region is normalised to 0..1 of the video's displayed content box (respect letterboxing with `object-fit: contain`). Pure geometry helpers go in `split_mark.js` with node tests.
   - **The profile's references** show as small thumbnails with their region outlined.
   - **"Find cuts":** shown when the profile is usable. It POSTs detect, shows the detect job's progress by polling the state every 2 s, and reloads the plan when the proposal arrives. With a non-detected plan that has cuts, confirm first ("Replace your cuts with the detected ones?").
   - **Detected cuts:** a cut in `detected` (match `at_s` within 0.05 s) gets a confidence badge ("sure" ≥ 0.75, "check" ≥ 0.4, else "unsure") and its snap kind ("black", "scene change", "at the title card"), in the parts list and on the strip card. OCR titles are already in the segments.
   - When `awaiting_split`: a notice "Hidden from the kids until you approve the split", with a "Publish as one video" button (publish-whole).
4. **Show page:** a "Splitting" card, linked from the split page. It has the references (thumbnail, region outline, delete), the profile form (threshold with a hint "lower is stricter", length hint, snap window, OCR, auto-detect: "Find episodes in new long videos automatically; they stay hidden until you approve them"), and a note when there are no references yet.
5. **Library "Splits to review":** a "Detected" badge for `origin == "detected"`, and "Hidden until approved" for `awaiting_split`. **Dashboard:** a small count of proposals in `review`, linking to the library section, if the dashboard has a natural place for it (it's optional; skip it if it complicates the live dashboard).
6. **Tests:**
   - the reference route (region stored, hash present, image served);
   - detect route errors and success;
   - the profile form save and errors;
   - reference delete;
   - publish-whole;
   - state additions;
   - the show-page card;
   - library badges;
   - node tests for the `split_mark.js` geometry (letterbox and pillarbox, drag in any direction, clamping).

   Check the split page at 375 and 1280 px with headless Chromium: Playwright's Chrome isn't installed, so use `~/.cache/ms-playwright/chromium-1228/chrome-linux64/chrome` over CDP from a node script, against a dev server you start on a free port with a seeded DB. Report what you checked.
