-- Profiles to grant the show once a download publishes (step 18, AD-8): chosen when approving from the
-- inbox or adding by URL, before the show may exist. Applied when the episode joins its show.
CREATE TABLE source_video_profile (
    source_video_id INTEGER NOT NULL REFERENCES source_video(id) ON DELETE CASCADE,
    profile_id      INTEGER NOT NULL REFERENCES profile(id) ON DELETE CASCADE,
    PRIMARY KEY (source_video_id, profile_id)
);
