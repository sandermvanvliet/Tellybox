-- Reader UI and per-profile TV (step 16, KA-11, PB-6).

-- Kid app style for this profile: 'icons' (no reading needed, the default, KA-2) or 'text' (reader UI, KA-11).
ALTER TABLE profile ADD COLUMN ui_mode TEXT NOT NULL DEFAULT 'icons' CHECK (ui_mode IN ('icons', 'text'));
-- This profile's default TV (cast_device.uuid); NULL means the global selected device (PB-6).
ALTER TABLE profile ADD COLUMN cast_device_uuid TEXT;
