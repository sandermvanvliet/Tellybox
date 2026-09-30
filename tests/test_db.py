import threading

from tellybox import db


def test_migrations_apply_once(tmp_path):
    conn = db.open_db(tmp_path / "t.db")
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    assert version == db.migrations()[-1][0]
    assert db.migrate(conn) == version  # idempotent
    assert conn.execute("SELECT count(*) FROM profile").fetchone()[0] == 1  # household profile (v1)


def test_concurrent_startup_migrates_once(tmp_path):  # web, cast and worker start together
    # Each round starts from a fresh file: switching it to WAL is where concurrent starts collided.
    for round_ in range(20):
        path = tmp_path / f"t{round_}.db"
        errors = []

        def start():
            try:
                db.open_db(path)
            except Exception as exc:  # pragma: no cover - failure path
                errors.append(exc)

        threads = [threading.Thread(target=start) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert errors == []
        assert db.connect(path).execute("SELECT count(*) FROM profile").fetchone()[0] == 1


def test_per_thread_connection_keeps_threads_apart(tmp_path):
    """The web service runs sync handlers in a thread pool; one shared connection mixed their queries."""
    path = tmp_path / "t.db"
    db.open_db(path).close()
    conn = db.PerThreadConnection(path)
    conn.execute("BEGIN IMMEDIATE")
    seen = {}

    def other_thread():
        seen["in_transaction"] = conn.in_transaction
        seen["reset_time"] = conn.execute("SELECT reset_time FROM settings WHERE id = 1").fetchone()["reset_time"]

    t = threading.Thread(target=other_thread)
    t.start()
    t.join()
    conn.execute("COMMIT")
    assert seen == {"in_transaction": False, "reset_time": "04:00"}


def test_migration_008_keeps_existing_jobs(tmp_path):  # SponsorBlock rebuilds the job table
    conn = db.connect(tmp_path / "t.db")
    for version, _name, sql in db.migrations():
        if version <= 7:
            for statement in db._split_sql(sql):
                conn.execute(statement)
            conn.execute(f"PRAGMA user_version = {version}")
    ts = "2026-09-28T10:00:00.000+00:00"
    conn.execute(
        """INSERT INTO job (type, target_id, status, attempts, error, run_after, created_at, updated_at)
           VALUES ('download', 5, 'failed', 3, 'boom', ?, ?, ?), ('update_ytdlp', NULL, 'queued', 0, NULL, ?, ?, ?)""",
        (ts, ts, ts, ts, ts, ts),
    )
    assert db.migrate(conn) == db.migrations()[-1][0] >= 8

    rows = [tuple(r) for r in conn.execute("SELECT id, type, target_id, status, attempts, error FROM job ORDER BY id")]
    assert rows == [(1, "download", 5, "failed", 3, "boom"), (2, "update_ytdlp", None, "queued", 0, None)]
    for job_type in ("sb_recheck", "redownload"):
        conn.execute("INSERT INTO job (type, run_after, created_at, updated_at) VALUES (?, ?, ?, ?)", (job_type, ts, ts, ts))
    assert conn.execute("SELECT count(*) FROM position_shift").fetchone()[0] == 0
    assert conn.execute("SELECT sponsorblock_categories FROM settings").fetchone()[0] == "sponsor,selfpromo,interaction"
    assert conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'job_queue'").fetchone()
