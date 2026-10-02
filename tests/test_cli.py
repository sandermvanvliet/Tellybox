"""`tellybox` command line: migrate and dev helpers."""

import shutil
from pathlib import Path

import pytest

from tellybox import cli, db, library


@pytest.fixture
def env(tmp_path, monkeypatch):
    data, media = tmp_path / "data", tmp_path / "media"
    media.mkdir()
    monkeypatch.setenv("TELLYBOX_DATA_DIR", str(data))
    monkeypatch.setenv("TELLYBOX_MEDIA_DIR", str(media))
    monkeypatch.setenv("TELLYBOX_MEDIA_BASE_URL", "http://127.0.0.1:8080")  # skips LAN IP detection
    monkeypatch.delenv("TELLYBOX_DB", raising=False)
    monkeypatch.delenv("TELLYBOX_SECRET_FILE", raising=False)
    return data, media


@pytest.fixture
def fake_ffprobe(monkeypatch):
    durations = {}
    monkeypatch.setattr(cli, "probe_duration", lambda path: durations.get(Path(path).name))
    return durations


def test_migrate(env, capsys):
    data, _ = env
    assert cli.main(["migrate"]) == 0
    latest = max(v for v, _, _ in db.migrations())
    assert str(latest) in capsys.readouterr().out
    conn = db.connect(data / "tellybox.db")
    assert conn.execute("PRAGMA user_version").fetchone()[0] == latest


def test_seed_registers_mp4s_sorted(env, fake_ffprobe, capsys):
    data, media = env
    show_dir = media / "cartoons" / "bluey"
    show_dir.mkdir(parents=True)
    for name in ["ep02.mp4", "ep01.mp4", "notes.txt", "ep10.mp4"]:
        (show_dir / name).write_bytes(b"x")
    fake_ffprobe.update({"ep01.mp4": 61.5, "ep02.mp4": 62.0})

    assert cli.main(["dev", "seed", str(show_dir)]) == 0
    out = capsys.readouterr().out

    conn = db.connect(data / "tellybox.db")
    show = conn.execute("SELECT id, name, autoplay FROM show").fetchone()
    assert show["name"] == "bluey"
    assert show["autoplay"] == 1
    eps = library.list_episodes(conn, show["id"])
    assert [(e.title, e.file_path, e.duration_s) for e in eps] == [
        ("ep01", "cartoons/bluey/ep01.mp4", 61.5),
        ("ep02", "cartoons/bluey/ep02.mp4", 62.0),
        ("ep10", "cartoons/bluey/ep10.mp4", None),
    ]
    assert f"show {show['id']}" in out
    for e in eps:
        assert f"episode {e.id}" in out


def test_seed_with_name_and_no_autoplay(env, fake_ffprobe):
    data, media = env
    (media / "d").mkdir()
    (media / "d" / "a.mp4").write_bytes(b"x")
    assert cli.main(["dev", "seed", str(media / "d"), "--show", "My Show", "--no-autoplay"]) == 0
    conn = db.connect(data / "tellybox.db")
    row = conn.execute("SELECT name, autoplay FROM show").fetchone()
    assert (row["name"], row["autoplay"]) == ("My Show", 0)


def test_seed_rejects_dir_outside_media(env, tmp_path, fake_ffprobe, capsys):
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / "a.mp4").write_bytes(b"x")
    assert cli.main(["dev", "seed", str(outside)]) != 0
    assert "media dir" in capsys.readouterr().err


def test_seed_rejects_empty_dir(env, fake_ffprobe, capsys):
    _, media = env
    (media / "empty").mkdir()
    assert cli.main(["dev", "seed", str(media / "empty")]) != 0
    assert "no .mp4" in capsys.readouterr().err


def test_make_clips_calls_script(env, monkeypatch, capsys):
    _, media = env
    calls = []
    monkeypatch.setattr(cli.subprocess, "run", lambda cmd, check: calls.append(cmd))
    assert cli.main(["dev", "make-clips", "3", "--seconds", "5"]) == 0
    assert len(calls) == 3
    script, out, seconds, label = calls[1]
    assert Path(script).name == "make_test_clip.sh"
    assert Path(out) == media / "dev-show" / "ep02.mp4"
    assert (seconds, label) == ("5", "EP 2")


def test_make_clips_default_seconds_and_out(env, monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(cli.subprocess, "run", lambda cmd, check: calls.append(cmd))
    assert cli.main(["dev", "make-clips", "1", "--out", str(tmp_path / "x")]) == 0
    assert calls[0][1:] == [str(tmp_path / "x" / "ep01.mp4"), "120", "EP 1"]


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="needs ffmpeg")
def test_make_clips_and_seed_end_to_end(env, capsys):
    data, media = env
    assert cli.main(["dev", "make-clips", "2", "--seconds", "1"]) == 0
    assert sorted(p.name for p in (media / "dev-show").iterdir()) == ["ep01.mp4", "ep02.mp4"]
    assert cli.main(["dev", "seed", str(media / "dev-show")]) == 0
    conn = db.connect(data / "tellybox.db")
    durations = [r[0] for r in conn.execute("SELECT duration_s FROM episode ORDER BY sort_order")]
    assert len(durations) == 2
    assert all(d == pytest.approx(1.0, abs=0.2) for d in durations)


def test_probe_duration_missing_file(tmp_path):
    if not shutil.which("ffprobe"):
        pytest.skip("needs ffprobe")
    assert cli.probe_duration(tmp_path / "nope.mp4") is None


def test_backup_writes_file_and_prints_it(env, tmp_path, capsys):
    assert cli.main(["migrate"]) == 0
    capsys.readouterr()
    dest = tmp_path / "backups"

    assert cli.main(["backup", str(dest)]) == 0

    files = list(dest.glob("tellybox-*.db"))
    assert len(files) == 1
    assert files[0].name in capsys.readouterr().out


def test_backup_respects_keep(env, tmp_path):
    assert cli.main(["migrate"]) == 0
    dest = tmp_path / "backups"
    dest.mkdir()
    (dest / "tellybox-20260101-0000.db").write_text("old")
    (dest / "tellybox-20260102-0000.db").write_text("old")

    assert cli.main(["backup", str(dest), "--keep", "2"]) == 0

    assert len(list(dest.glob("tellybox-*.db"))) == 2


def test_backup_failed_integrity_check_exits_nonzero_and_prunes_nothing(env, tmp_path, monkeypatch, capsys):
    assert cli.main(["migrate"]) == 0
    dest = tmp_path / "backups"
    dest.mkdir()
    (dest / "tellybox-20260101-0000.db").write_text("kept")
    monkeypatch.setattr(cli.backup, "_integrity_check", lambda path: "corrupted")

    assert cli.main(["backup", str(dest)]) == 1

    assert "error" in capsys.readouterr().err.lower()
    assert [p.name for p in dest.glob("tellybox-*.db")] == ["tellybox-20260101-0000.db"]


def test_no_command_prints_help(capsys):
    assert cli.main([]) != 0


def test_reset_password_clears_a_ui_password_and_prints_a_code(env, capsys):  # DP-4
    from datetime import UTC, datetime

    from tellybox import auth
    data, _ = env
    conn = db.open_db(data / "tellybox.db")
    code = auth.new_setup_code(conn, datetime.now(UTC))
    assert auth.set_ui_password(conn, "chosen in the browser")
    auth.create_session(conn, datetime.now(UTC))
    assert cli.main(["reset-password"]) == 0
    out = capsys.readouterr().out
    assert auth.is_locked(conn)
    assert conn.execute("SELECT count(*) FROM admin_session").fetchone()[0] == 0
    printed = out.split("Setup code: ")[1].split()[0]
    assert printed != code and auth.check_setup_code(conn, printed)


def test_reset_password_leaves_an_env_password_alone(env, monkeypatch, capsys):
    monkeypatch.setenv("TELLYBOX_ADMIN_PASSWORD", "from env")
    assert cli.main(["reset-password"]) == 1
    assert "TELLYBOX_ADMIN_PASSWORD" in capsys.readouterr().out
