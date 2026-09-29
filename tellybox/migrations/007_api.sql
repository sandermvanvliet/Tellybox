-- Admin API for Home Assistant (v2.1, HA-1..HA-8).

-- Long-lived API tokens (HA-1). Only a SHA-256 of the secret is stored; the secret is shown once.
-- scopes: space-separated, "read" or "read control". Revoked tokens stay listed.
CREATE TABLE api_token (
    id           INTEGER PRIMARY KEY,
    name         TEXT    NOT NULL,
    token_hash   TEXT    NOT NULL UNIQUE,
    scopes       TEXT    NOT NULL,
    created_at   TEXT    NOT NULL,
    last_used_at TEXT,
    revoked_at   TEXT
);

-- A stable id for this installation (HA-6), so integrations can tell instances apart.
ALTER TABLE settings ADD COLUMN instance_id TEXT;
UPDATE settings SET instance_id = lower(hex(randomblob(16))) WHERE instance_id IS NULL;

-- Who applied an override (HA-7): NULL = the admin pages, otherwise the API token's name at the time.
ALTER TABLE override_log ADD COLUMN source TEXT;
