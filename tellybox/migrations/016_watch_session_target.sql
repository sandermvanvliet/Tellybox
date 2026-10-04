-- Watching in the app (step 17, PB-7, PB-8, WT-12): where a watch session played.
-- 'tv' is the Chromecast; 'device' is a browser tab, named by a short label such as "iPhone Safari".
ALTER TABLE watch_session ADD COLUMN target TEXT NOT NULL DEFAULT 'tv' CHECK (target IN ('tv', 'device'));
ALTER TABLE watch_session ADD COLUMN device_label TEXT;
