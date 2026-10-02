-- First-run admin password (DP-4): the password can be chosen in the browser, guarded by a setup code.

-- Where the stored hash came from: 'env' (TELLYBOX_ADMIN_PASSWORD[_FILE], always wins) or 'ui' (setup page).
ALTER TABLE settings ADD COLUMN admin_password_source TEXT;
-- SHA-256 of the current setup code (logged at web start while no password exists); never the code itself.
ALTER TABLE settings ADD COLUMN admin_setup_code_hash TEXT;
ALTER TABLE settings ADD COLUMN admin_setup_code_created_at TEXT;

UPDATE settings SET admin_password_source = 'env' WHERE admin_password_hash IS NOT NULL;
