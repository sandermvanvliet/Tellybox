-- Smart splitting (v6, ES-3..ES-6, ES-9, ES-10): a splitting profile per show.

CREATE TABLE split_profile (
    show_id         INTEGER PRIMARY KEY REFERENCES show(id) ON DELETE CASCADE,
    match_threshold INTEGER NOT NULL DEFAULT 6,    -- ES-4: max Hamming distance of a 64-bit dHash to a reference
    length_hint_s   REAL,                          -- ES-5: approximate episode length; NULL = no hint
    snap_window_s   REAL    NOT NULL DEFAULT 30,   -- ES-6: look this far back for a scene change or black
    ocr             INTEGER NOT NULL DEFAULT 0,    -- ES-9: read titles from the title card
    ocr_region_json TEXT,                          -- [x, y, w, h], 0..1 of the frame; NULL = the whole frame
    auto_detect     INTEGER NOT NULL DEFAULT 0,    -- ES-10: detect new compilations after download
    updated_at      TEXT    NOT NULL
);

-- ES-3: the title cards the admin marked. The image is kept for the show page; matching uses the hash.
CREATE TABLE split_reference (
    id              INTEGER PRIMARY KEY,
    show_id         INTEGER NOT NULL REFERENCES show(id) ON DELETE CASCADE,
    image_path      TEXT    NOT NULL,              -- relative to the media dir: split/show-<id>-<ts>.jpg
    region_json     TEXT,                          -- [x, y, w, h], 0..1 of the frame; NULL = the whole frame
    card_hash       TEXT    NOT NULL,              -- hex dHash of the region (detect.reference_hash)
    source_video_id INTEGER REFERENCES source_video(id) ON DELETE SET NULL,
    at_s            REAL,                          -- where in that source it was marked
    created_at      TEXT    NOT NULL
);
CREATE INDEX split_reference_show ON split_reference (show_id);

-- Detection details per cut of a detected proposal, for the review screen (ES-7):
-- [{"at_s", "title_hit_s", "confidence", "snapped", "title"}]
ALTER TABLE split_proposal ADD COLUMN detected_json TEXT;

-- ES-10, A-22: a compilation found by automatic detection stays hidden until its split is
-- approved, or the admin publishes it whole.
ALTER TABLE source_video ADD COLUMN awaiting_split INTEGER NOT NULL DEFAULT 0;
