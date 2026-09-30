-- SponsorBlock (v3, SB-1..SB-5): segments are cut out of the file at download.

-- SB-2: enabled categories (yt-dlp keys, comma separated). '' = SponsorBlock off.
ALTER TABLE settings ADD COLUMN sponsorblock_categories TEXT NOT NULL DEFAULT 'sponsor,selfpromo,interaction';

-- SB-2: per show. NULL = the global setting, '' = off, otherwise its own categories.
ALTER TABLE show ADD COLUMN sponsorblock_categories TEXT;

-- What was done to the current file (SB-1, SB-3, SB-4).
-- categories the current file was cut for; NULL = not cut
ALTER TABLE source_video ADD COLUMN sb_categories TEXT;
-- removed [{"category", "start_s", "end_s"}], original timeline
ALTER TABLE source_video ADD COLUMN sb_segments_json TEXT;
-- total time removed
ALTER TABLE source_video ADD COLUMN sb_removed_s REAL;
-- cut: segments removed · none: no segments in the enabled categories · unreachable: downloaded uncut (SB-5)
-- off: SponsorBlock off for this show · admin_off: "Download again without SponsorBlock" (SB-4), no re-checks
ALTER TABLE source_video ADD COLUMN sb_status TEXT
    CHECK (sb_status IN ('cut', 'none', 'unreachable', 'off', 'admin_off'));
ALTER TABLE source_video ADD COLUMN sb_checked_at TEXT;
-- SB-3: daily re-checks until then
ALTER TABLE source_video ADD COLUMN sb_recheck_until TEXT;

-- New job types: sb_recheck (SB-3) and redownload (replace a published file, SB-3/SB-4).
-- SQLite can't alter a CHECK constraint, so the table is rebuilt. Nothing references job.
CREATE TABLE job_new (
    id           INTEGER PRIMARY KEY,
    type         TEXT    NOT NULL CHECK (type IN ('download', 'update_ytdlp', 'sb_recheck', 'redownload')),
    target_id    INTEGER,          -- source_video.id for download, sb_recheck and redownload jobs
    status       TEXT    NOT NULL DEFAULT 'queued'
                 CHECK (status IN ('queued', 'downloading', 'processing', 'ready', 'failed')),
    progress     REAL,
    error        TEXT,
    attempts     INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 4,
    run_after    TEXT    NOT NULL,
    heartbeat_at TEXT,
    created_at   TEXT    NOT NULL,
    updated_at   TEXT    NOT NULL,
    finished_at  TEXT
);
INSERT INTO job_new SELECT id, type, target_id, status, progress, error, attempts, max_attempts, run_after,
    heartbeat_at, created_at, updated_at, finished_at FROM job;
DROP TABLE job;
ALTER TABLE job_new RENAME TO job;
CREATE INDEX job_queue ON job (status, run_after, id);

-- SB-3: saved positions to move after a file was replaced. The worker queues; the cast
-- service, the only writer of playback_position, applies and deletes.
CREATE TABLE position_shift (
    id             INTEGER PRIMARY KEY,
    episode_id     INTEGER NOT NULL REFERENCES episode(id) ON DELETE CASCADE,
    old_cuts_json  TEXT    NOT NULL,   -- segments removed from the replaced file, original timeline
    new_cuts_json  TEXT    NOT NULL,   -- segments removed from the new file, original timeline
    created_at     TEXT    NOT NULL
);
