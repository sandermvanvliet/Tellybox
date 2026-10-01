-- Episode splitting (v5 manual: ES-1, ES-2, ES-7, ES-8; v6 smart reuses the proposal and the detect job).

-- One proposal per source video: the cut plan the admin edits, reviews and approves (ES-7).
CREATE TABLE split_proposal (
    id              INTEGER PRIMARY KEY,
    source_video_id INTEGER NOT NULL UNIQUE REFERENCES source_video(id) ON DELETE CASCADE,
    -- draft: being edited · review: proposed (chapters or detection), not yet looked at · approved: split job queued
    -- cutting: split job running · done: the episodes are the cut ones · failed: the split job failed (error)
    status          TEXT    NOT NULL DEFAULT 'draft'
                    CHECK (status IN ('draft', 'review', 'approved', 'cutting', 'done', 'failed')),
    -- where the cuts came from: chapters (ES-1), manual (ES-2), detected (ES-4, v6)
    origin          TEXT    NOT NULL DEFAULT 'manual' CHECK (origin IN ('chapters', 'manual', 'detected')),
    -- [{"start_s", "end_s", "title", "keep"}], contiguous from 0 to the file's duration, file timeline
    segments_json   TEXT    NOT NULL,
    delete_source   INTEGER NOT NULL DEFAULT 0,  -- ES-8, A-21: delete the source file once the parts are in place
    error           TEXT,
    created_at      TEXT    NOT NULL,
    updated_at      TEXT    NOT NULL,
    approved_at     TEXT
);

-- New job types: split (ES-8) and detect (ES-4, v6; added now so v6 needs no second rebuild).
-- SQLite can't alter a CHECK constraint, so the table is rebuilt as in 008. Nothing references job.
CREATE TABLE job_new (
    id           INTEGER PRIMARY KEY,
    type         TEXT    NOT NULL
                 CHECK (type IN ('download', 'update_ytdlp', 'sb_recheck', 'redownload', 'split', 'detect')),
    target_id    INTEGER,          -- source_video.id for every type except update_ytdlp
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
