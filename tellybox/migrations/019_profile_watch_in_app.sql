-- Watching in the app (step 17, AD-7, KA-13): whether this profile's kid app may play episodes on the device
-- itself instead of the TV. Off by default; the kid app then hides the TV/phone toggle.
ALTER TABLE profile ADD COLUMN watch_in_app INTEGER NOT NULL DEFAULT 0 CHECK (watch_in_app IN (0, 1));
