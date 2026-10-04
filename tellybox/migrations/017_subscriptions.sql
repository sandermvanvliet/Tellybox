-- Channel subscriptions (CS-1..CS-9, step 11): subscriptions, the approval inbox and the seen-video baseline.
CREATE TABLE subscription (
    id              INTEGER PRIMARY KEY,
    channel_id      TEXT    NOT NULL UNIQUE,   -- stable UC... id, resolved at subscribe time; the handle is display only
    channel_name    TEXT    NOT NULL,
    channel_url     TEXT    NOT NULL,
    show_id         INTEGER REFERENCES show(id) ON DELETE SET NULL,   -- NULL: the channel's own show (CI-4, CS-9)
    include_shorts  INTEGER NOT NULL DEFAULT 0,                        -- A-31
    paused          INTEGER NOT NULL DEFAULT 0,                        -- CS-4
    baseline_at     TEXT    NOT NULL,          -- display only; the baseline itself is subscription_seen (A-32)
    last_checked_at TEXT,
    last_ok_at      TEXT,
    last_error      TEXT,                      -- CS-7
    failing_since   TEXT,                      -- start of the current run of failed checks; NULL while healthy
    created_at      TEXT    NOT NULL
);

-- Rows outlive their subscription (subscription_id becomes NULL) so a rejection is remembered (A-34).
CREATE TABLE inbox_item (
    id              INTEGER PRIMARY KEY,
    subscription_id INTEGER REFERENCES subscription(id) ON DELETE SET NULL,
    youtube_id      TEXT    NOT NULL UNIQUE,   -- CS-5: one item per video, whatever its status
    url             TEXT    NOT NULL,
    title           TEXT    NOT NULL,
    channel_name    TEXT,
    duration_s      REAL,
    thumbnail_url   TEXT,
    published_at    TEXT,
    status          TEXT    NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'approved', 'rejected')),
    warning         TEXT,                      -- "may not be downloadable" (CS-5)
    received_at     TEXT    NOT NULL,
    decided_at      TEXT
);
CREATE INDEX inbox_item_status ON inbox_item (status, received_at);
CREATE INDEX inbox_item_subscription ON inbox_item (subscription_id);

-- Videos a subscription has handled or that were already there when it started (A-32).
CREATE TABLE subscription_seen (
    subscription_id INTEGER NOT NULL REFERENCES subscription(id) ON DELETE CASCADE,
    youtube_id      TEXT    NOT NULL,
    PRIMARY KEY (subscription_id, youtube_id)
);

ALTER TABLE settings ADD COLUMN subscription_check_hours INTEGER NOT NULL DEFAULT 6;   -- CS-2
-- source_video.show_id (002) doubles as the forced show: set before the download, it wins over the channel lookup.
