-- "Check now" (CS-2): the admin pages stamp a request, the worker's next step runs the check and clears it.
ALTER TABLE subscription ADD COLUMN check_requested_at TEXT;
