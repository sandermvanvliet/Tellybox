# Step 12 (manual splitting): subagent briefs

Plan: `docs/plans/step12-14-splitting.md`. Branch `step12/manual-split`. The contract is committed. Build on it, and don't change it without saying so in your report.

## Contract (done)

- **Migration `009_splitting.sql`:**
  - `split_proposal`: one row per source video, with `status`, `origin`, `segments_json`, `delete_source`, `error` and the timestamps;
  - the `job` table rebuilt with the types `split` and `detect`.
- **`tellybox/splitting.py`** (pure), with tests in `tests/test_splitting.py`:
  - `Segment(start_s, end_s, title="", keep=True)`, on the file timeline (after the SponsorBlock cuts);
  - `whole`, `segments_from_cuts`, `cuts_of`, `segments_from_chapters` (ES-1);
  - `chapters_on_file`: maps old rows' original-timeline chapters onto the cut file;
  - `check_shape` (what a saved draft must satisfy) and `validate` (what approval and cutting need). Both raise `SplitInvalid` with a translated message;
  - `kept_titles(segments, fallback)`;
  - `episode_file_name(youtube_id, job_id, n)` gives `<ytid>-s<job>-<nn>.mp4`, so a re-split never overwrites the parts it replaces;
  - `dumps`/`loads`/`from_dict`;
  - the constants `STATUSES`, `ORIGINS`, `EDITABLE`, `MIN_KEPT_S`.
- **`tellybox/library.py`:**
  - `SplitProposal`, which has `.editable`;
  - `get_split(conn, source_id)`. If nothing is saved, it returns an unsaved proposal (`stored=False`) built from the chapters (2 or more), otherwise the whole video. It returns None when the source isn't `ready`;
  - `save_split(conn, source_id, segments, *, now, origin=None)`: upsert as `draft`. A `done` proposal becomes a draft again, which is a re-split;
  - `approve_split(conn, source_id, *, delete_source, now) -> job_id`: validates, sets `approved`, and enqueues `SPLIT` with `max_attempts=2`;
  - `discard_split` (draft, review or failed only);
  - `mark_split(conn, source_id, "cutting"|"done"|"failed", *, now, error=None)`, for the worker;
  - `list_splits_for_review`, which gives `SplitListItem`s;
  - `list_source_episodes(conn, source_id)` and `is_split(conn, source_id)`;
  - the exceptions `SplitLocked` (a split job is queued or running; or discarding a done proposal) and `SourceGone` (the source isn't ready, or its file was deleted, A-21).
- **`tellybox/jobs.py`:** `JobType.SPLIT` and `JobType.DETECT` (DETECT is for v6). Both are claimed as `processing`.
- **`tellybox/media_format.cut(src, dst, start_s, end_s, *, on_progress=None, nice=10, timeout=None)`:** a frame-accurate re-encode with the same settings as `convert`, through a temp file then `os.replace`. It doesn't verify; call `verify` afterwards. A test checks that a cut starts on the exact frame of a clip with a single keyframe.
- **Proposal status lifecycle:**
  1. `draft`/`review` (being edited);
  2. `approved` (job queued);
  3. `cutting` (job running);
  4. `done` or `failed` (with `error`).

  A queued retry after a retryable failure goes back to `approved`.

**Rules for everyone:**
- Follow CLAUDE.md, including NF-13: every interface string is translatable. Run `scripts/i18n.sh` and translate the new entries in `tellybox/locale/{nl,de}` (informal "je"/"du"). `tests/test_i18n.py` must pass.
- Match the surrounding code style and comment density.
- Run the whole suite (`.venv/bin/pytest -q`, about 2 min) before reporting.
- Don't commit. Report the files you changed, anything you changed in the contract, and anything open.

---

## A: worker and ingest (`tellybox/ingest.py`, `tellybox/worker/`, small fixes in `tellybox/library.py`)

1. **`JobRunner._run_split(job)`** (ES-8), dispatched from `JobRunner.run`. `DETECT` fails as not retryable ("title-card detection arrives in v6").
   1. **Checks:**
      - load the source (`get_source_video`) and `library.get_split`;
      - the proposal must be stored, with status `approved` or `cutting`, and the source file must exist. Otherwise `jobs.fail(..., retryable=False)`.
   2. **Defer** (`_defer_if_playing`) while any of `library.list_source_episodes` is loaded on the TV, or while the cast service can't be reached.
   3. `mark_split(..., "cutting")`.
   4. **Validate** against the *probed* duration of the source file (`splitting.validate`), since a redownload may have changed the file. `SplitInvalid` fails the job as not retryable, and the proposal becomes `failed` with the message.
   5. **Cut** in `self.tmp_dir(job.id)`. For kept part n (1-based), `media_format.cut` into the tmp dir, then `verify`. The progress is weighted by part length over the total kept length, through `self._progress(job.id)`. The thumbnail is `images.grab_frame(part, min(3.0, length/4))`, written to the tmp dir.
   6. **Publish** in one `BEGIN IMMEDIATE` transaction (NF-8, pattern `_publish`/`_swap`):
      - move each part to `shows/<show_id>/<splitting.episode_file_name(ytid, job.id, n)>` with `FILE_MODE`;
      - the show is the show of the source's current episodes, else `source.show_id`;
      - insert the episodes: `start_s`/`end_s` set (also when `start_s == 0`, since it's the SB-6 marker), the title from `splitting.kept_titles(segments, source.title)`, `duration_s` from the probe, `source_video_id`;
      - hidden when the source is held (`publish == 'hold'`) or every old episode was hidden;
      - the new parts take the place of the old episodes in the show's order: build the show's id list, put the new ids at the first old episode's index, drop the old ids, and renumber `sort_order` 0..n-1;
      - thumbnails: `images.save_episode_thumbnail(media_dir, episode_id, data)` and set `thumbnail_path`;
      - delete the old episodes. Positions and `position_shift` go by cascade, and `watch_session` keeps its rows (SET NULL);
      - with `delete_source`, set `source_video.file_path = NULL`;
      - `mark_split(..., "done")` and `jobs.complete`.

      On any exception, roll back and unlink every file moved or written.
   7. **After the commit**, `library._remove_unreferenced(conn, media_dir, [old episode files and thumbnails, the source file])`. The source's own thumbnail stays (it's still referenced).
   8. **On a failure** (`MediaError`, `ImageError` and so on): `jobs.fail(..., retryable=True)`. If the job is now `failed`, `mark_split(..., "failed", error=...)`, otherwise `UPDATE … status='approved'`. Clean up tmp in `finally`. The old episodes stay playable throughout.
2. **SB-6:**
   - `request_redownload` raises a new `ingest.SourceSplit` when `library.is_split` is true or a SPLIT job is pending;
   - `_enqueue_rechecks` (worker) skips split sources (`NOT EXISTS (… start_s IS NOT NULL)`);
   - `_run_redownload` and `_run_sb_recheck` keep their no-op, and log why.
3. **Chapters (SB-1/ES-1):**
   - `_publish` stores the download's chapters when there are any (`chapters_json = COALESCE(?, chapters_json)`). They're already shifted by yt-dlp;
   - the redownload updates `chapters_json` the same way.
4. **`library.list_held_downloads`:** one row per source. `episode_id` becomes `(SELECT MIN(id) …)` instead of the LEFT JOIN.
5. **Tests** (`tests/test_ingest.py` or a new `tests/test_split_job.py`; real ffmpeg on small lavfi clips, as `h264_clip` does):
   - a 3-part split with one part left out: durations ±0.1 s, titles (own and fallback), `start_s`/`end_s`, sort order in the middle of a show, thumbnails written, the old episode gone with its positions, the old file kept when `delete_source=False`;
   - `delete_source=True`: the file is gone, `file_path` NULL, and the source thumbnail is kept;
   - a held source gives hidden parts;
   - a re-split replaces the earlier parts and removes their files;
   - deferral while playing or when the cast service is unreachable;
   - a cut failure (monkeypatch `media_format.cut` to raise on part 2): nothing moved, the old episode is intact, the proposal is `approved` and then `failed` after the last attempt;
   - a duration mismatch gives `failed` with the message;
   - SB-6: `request_redownload` raises `SourceSplit`, and rechecks are not enqueued for split sources;
   - the chapters fix;
   - held list deduplication.

---

## B: admin routes and templates (new `tellybox/web/admin/split_pages.py`, templates, `_labels.html`, `jobs_page.py`, `library_pages.py` and `episode.html` for the links, catalogs)

Register `split_pages` in `PAGE_MODULES` (`tellybox/web/admin/__init__.py`).

1. **Routes** (all behind `AdminGuard`; unsafe methods are Origin-checked already):
   - `GET /admin/sources/{id}/video.mp4`: `FileResponse` of the source file. It handles Range, which the scrub player needs. Resolve the path with `_media_path`. 404 when the source is missing or `file_path` is NULL. `Cache-Control: no-store`.
   - `GET /admin/sources/{id}/frame.jpg?t=<seconds>`: `images.grab_frame` on the source file, the same as `/admin/episodes/{id}/frame.jpg`. Frame-accurate (`-ss` before `-i` while decoding).
   - `GET /admin/api/splits/{id}`: returns **the split state** (below), or 404 when `get_split` is None.
   - `PUT /admin/api/splits/{id}`, with a JSON body `{"segments": [...], "origin"?: "chapters"|"manual"}`:
     - `splitting.from_dict` for each segment, then `library.save_split`, which returns the state;
     - `SplitInvalid` gives 422 `{"error": msg}`;
     - `SplitLocked` or `SourceGone` gives 409 `{"error": msg}`;
     - KeyError gives 404.
   - `GET /admin/sources/{id}/split`: the split page (`split.html`). 404 when `get_split` is None.
   - `POST /admin/sources/{id}/split/approve`, a form with the `delete_source` checkbox:
     - `library.approve_split`, then `see_other(<split page>, flash=_("Splitting is queued. The parts appear when it's done."))`;
     - errors re-render the page with 422 and the error;
     - KeyError (nothing saved yet) shows "Save a plan first".
   - `POST /admin/sources/{id}/split/discard`: `library.discard_split`, then `see_other` to the episode page of the source's first episode, with a flash.
2. **The split state** is the JSON served by the API and embedded in the page as `<script type="application/json" id="split-state">`. Build both from one function in `split_pages.py`:
   ```json
   {
     "source_id": 12, "title": "…", "duration_s": 3600.0, "fps": 25.0,
     "video_url": "/admin/sources/12/video.mp4", "frame_url": "/admin/sources/12/frame.jpg",
     "status": "draft", "origin": "chapters", "stored": false, "editable": true, "has_file": true,
     "delete_source": false, "error": null, "updated_at": "…iso…" ,
     "segments": [{"start_s": 0.0, "end_s": 300.0, "title": "One", "keep": true}],
     "chapters": [{"start_s": 0.0, "end_s": 300.0, "title": "One"}],
     "job": {"id": 5, "status": "processing", "progress": 0.4}
   }
   ```
   - `fps` comes from `media_format.probe` (default 25 when unknown). Cache the probe per source id and path for the life of the process, so autosave doesn't probe each time.
   - `job` is the newest SPLIT job for this source while it's queued or running, else null.
3. **`templates/split.html`** (extends `base.html`; `split.css` and `split.js` in the head and scripts blocks). C's JS depends on these hooks. Render the visible labels server-side with `_()`:
   - A root `<section id="split-editor" data-editable="true|false">`.
   - A `<video data-split-video src="…" preload="metadata" playsinline controls>`.
   - A step-button row `<button type="button" data-step="…">` with `data-step` values `-10`, `-1`, `-frame`, `+frame`, `+1`, `+10`. Labels: "−10 s", "−1 s", "◀ frame" ("previous frame" as `aria-label`), and so on.
   - `<output data-split-time>`.
   - `<button type="button" data-action="cut">` "Cut here".
   - `<button type="button" data-action="use-chapters">` "Use chapters" (render it only when there are 2 or more chapters).
   - `<ol data-split-segments>`, which C fills.
   - `<div data-split-strip>`, which C fills (ES-7).
   - `<p data-split-status role="status" aria-live="polite">`.
   - `<form data-split-approve method="post" action="…/approve">` with `<input type="checkbox" name="delete_source">` "Delete the original video after cutting (it can't be split again)", `<span data-split-estimate>`, and a submit button "Approve and cut".
   - `<form data-split-discard method="post" action="…/discard">` with a confirm, shown only when the proposal is stored and in draft, review or failed.
   - Status panels:
     - `approved`/`cutting`: "Cutting…" with `<div class="progress" data-split-progress><span></span></div>`;
     - `failed`: the error;
     - `done`: "Split into n episodes" with links to them;
     - `!has_file`: "The original video was deleted after splitting, so it can't be split again."
   - A `<noscript>` line: the editor needs JavaScript.
   - Use the `--nav` and `components` styles from `admin.css`. Keep the page shell phone-first; the editor layout is C's.
4. **Episode page** (`episode_context`, `episode.html`):
   - When the source is ready, has its file and isn't split: a link "Split into episodes".
   - When the episode is a part: "Part n of m of <source title>". There's a link "Split again" when the file exists, else the deleted note.
   - In the SponsorBlock card, for a split source, replace the redownload buttons with "This video was split into episodes; SponsorBlock no longer re-checks it." (SB-6).
   - `redownload_route` catches `ingest.SourceSplit`, which A defines; until A's branch is merged, check for it with `library.is_split` and show the same message as a flash error.
5. **Library page:** a "Splits to review" section from `library.list_splits_for_review`. Each row has the thumbnail (`/admin/img/episode/{episode_id}.jpg` when there is one), the title, a status label, `tn("%(n)d part", "%(n)d parts")` (server-side `ngettext`), and a link to the split page. Show the section only when it has items.
6. **Job labels:**
   - `_labels.html` `job_type`: `split` is "Split into episodes" and `detect` is "Find title cards";
   - `jobs_page._title` and `_label` include `split`/`detect` (they target a source);
   - `js_strings.py` gets the same labels if `dashboard.js`/`jobs.js` look them up there. Coordinate: B owns `jobs_page.py` and `_labels.html`; C owns `js_strings.py`, so list any strings you need there in your report.
7. **Tests** (`tests/web/admin/test_split_pages.py`, the `admin`/`admin_env` fixtures, and `_write_episode_video`-style real clips):
   - video route Range (206) and 404 after the source is deleted;
   - frame route;
   - GET/PUT state, including 422 and 409;
   - Origin required for PUT;
   - the page renders the hooks;
   - approve queues a job and shows the flash;
   - discard;
   - the episode-page links and SB-6 message;
   - the library review section;
   - job labels.

---

## C: split editor front end (`tellybox/web/admin/static/split.js`, `split_plan.js`, `split.css`, `js_strings.py`, catalogs)

The page shell and hooks come from B (section 3 above). The data comes from `#split-state` and `PUT data-api` (`/admin/api/splits/{id}`).

1. **`split_plan.js`:** pure ES module, with no DOM, holding the plan as a list of `{start_s, end_s, title, keep}`:
   - `addCut(plan, t)`: splits the segment containing `t`. Refused within 0.5 s of an existing cut or an edge. The new right part gets an empty title and the left part's keep;
   - `removeCut(plan, i)`: merges segment i with segment i-1 and keeps the left title;
   - `moveCut(plan, i, t)`: clamped between its neighbours ±0.5 s;
   - `nudge(plan, i, delta)`;
   - `setTitle`, `setKeep`;
   - `fromChapters(chapters, duration)`, matching `splitting.segments_from_chapters`;
   - `keptDuration`;
   - `problems(plan, duration)`: the client-side mirror of `splitting.validate`, giving a list of message ids for the approve button.

   Round to milliseconds. Test it with `node --test tests/js/split_plan.test.mjs` (Node 22 is on the dev box). Add a pytest wrapper, `tests/web/admin/test_split_plan_js.py`, that runs it, skipped when `node` is missing, so CI runs it as well.
2. **`split.js`** wires the hooks:
   - **Player:**
     - step buttons; a frame is `1/fps`;
     - keyboard: ←/→ ±1 s, Shift+←/→ ±10 s, `,`/`.` ±1 frame, `c` cuts, and Space plays or pauses. Ignore keys while focus is in an input;
     - `data-split-time` shows `m:ss.cc`.
   - **Segment list** (`data-split-segments`), one row per segment:
     - "Part n" with start–end;
     - a title input;
     - a keep toggle ("Keep" / "Left out");
     - the cut at its start with nudge buttons ±1 s / ±0.1 s, "Go to" (seeks) and "Remove cut".

     The row for the segment under the playhead is highlighted. Left-out parts are greyed out.
   - **Strip** (`data-split-strip`, ES-7): one card per segment. It shows the frame at the segment's start (`frame_url?t=`, lazy-loaded, refreshed when the cut moves, debounced) and its timestamp; tapping it seeks there. It scrolls horizontally on phones.
   - **Autosave:** debounced (800 ms) `PUT` with `{segments, origin}`. `origin` is "chapters" after "Use chapters", until the first manual edit, which sets "manual".
     - `data-split-status` shows "Saving…", "Saved", or the server's error.
     - On 409, reload the state and make the page read-only.
   - **"Use chapters"** replaces the plan, with a confirm when the plan has cuts.
   - **Approve form:** on submit, flush any pending save first, and block (showing the problems) when `problems()` isn't empty. `data-split-estimate` shows "Cutting takes up to about n min" (kept duration at real-time speed, NF-6, rounded up; `tn`).
   - **Read-only** (`data-editable="false"`): disable the inputs. While the status is approved or cutting, poll the API every 2 s, update `data-split-progress`, and reload on done or failed.
3. **`split.css`:**
   - phone-first (NF-12, a 4.7" screen): the video full width; step buttons in one wrapping row of large tap targets; the segment list under the player;
   - on screens 900 px and wider, the player and strip on the left and the list on the right;
   - use the existing CSS custom properties from `admin.css`.
4. **i18n:** every string in `split.js` goes through `t()`/`tn()` and is listed in `js_strings.py`. Run `scripts/i18n.sh`, then translate nl and de.
5. **Tests:** the node tests above. Also check by hand with `scripts/` or a dev server and a sample clip (`tellybox dev make-clips`), and report what you checked.
