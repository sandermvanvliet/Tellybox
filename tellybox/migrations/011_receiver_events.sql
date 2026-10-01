-- Receiver resilience (CR-6): why the Tellybox receiver failed, kept across deploys.
-- Written by the cast service only; the worker's purge deletes rows older than 21 days.
-- kind: launch_ok | launch_failed | refused | fallback | lost | recovered | recover_failed | page_error
CREATE TABLE receiver_event (
    id          INTEGER PRIMARY KEY,
    at          TEXT    NOT NULL,
    kind        TEXT    NOT NULL,
    detail      TEXT,
    duration_ms INTEGER,
    episode_id  INTEGER REFERENCES episode(id) ON DELETE SET NULL
);
CREATE INDEX receiver_event_at ON receiver_event (at);
