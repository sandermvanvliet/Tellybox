-- Which shows a profile can see (step 18, PR-5, A-37): an allow-list of (profile, show) pairs.
-- Episodes inherit from their show. A new show or profile has no rows, so it starts hidden.
CREATE TABLE profile_show (
    profile_id INTEGER NOT NULL REFERENCES profile(id) ON DELETE CASCADE,
    show_id    INTEGER NOT NULL REFERENCES show(id) ON DELETE CASCADE,
    PRIMARY KEY (profile_id, show_id)
);
CREATE INDEX idx_profile_show_show ON profile_show(show_id);

-- Upgrading changes nothing visible: every existing profile gets every existing show.
INSERT INTO profile_show (profile_id, show_id) SELECT p.id, s.id FROM profile p CROSS JOIN show s;
