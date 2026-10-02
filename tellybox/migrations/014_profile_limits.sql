-- Per-profile limits (issue #27): allowance and max session are inherit, custom or unlimited.
-- Existing rows keep the current behavior (custom), and new rows default to inherit.
-- Settings has the system-wide defaults for limit values.
ALTER TABLE profile ADD COLUMN allowance_mode TEXT NOT NULL DEFAULT 'inherit' CHECK (allowance_mode IN ('inherit', 'custom', 'unlimited'));
ALTER TABLE profile ADD COLUMN max_session_mode TEXT NOT NULL DEFAULT 'inherit' CHECK (max_session_mode IN ('inherit', 'custom', 'unlimited'));
UPDATE profile SET allowance_mode = 'custom', max_session_mode = 'custom';
ALTER TABLE settings ADD COLUMN default_allowance_min INTEGER NOT NULL DEFAULT 60;
ALTER TABLE settings ADD COLUMN default_max_session_min INTEGER NOT NULL DEFAULT 90;
