-- Tellybox receiver (v7, CR-1): the Cast app id of the household's own receiver, registered in
-- the Google Cast SDK Developer Console. NULL = Default Media Receiver only (the v1 behaviour).
ALTER TABLE settings ADD COLUMN receiver_app_id TEXT;
