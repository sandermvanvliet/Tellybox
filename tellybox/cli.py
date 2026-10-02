"""`tellybox` command line: schema migration and developer helpers.

    tellybox migrate
    tellybox add URL [--hold] [--yes]      preview, then queue the download (CI-1)
    tellybox jobs                          job status (CI-3)
    tellybox retry JOB_ID
    tellybox ytdlp-update                  queue a yt-dlp update (CI-5)
    tellybox backup DEST [--keep N]        consistent database backup (default: keep 14)
    tellybox dev make-clips N [--seconds S] [--out DIR]
    tellybox dev seed DIR [--show NAME] [--no-autoplay]
"""

from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

from tellybox import backup, db, ingest, jobs, library, privileges
from tellybox.config import Config
from tellybox.ytdlp import YtDlp, YtDlpError

log = logging.getLogger(__name__)

SCRIPT_NAME = "make_test_clip.sh"


def probe_duration(path: Path | str) -> float | None:
    """Duration in seconds via ffprobe, or None if it can't be read."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        return float(out)
    except (OSError, subprocess.CalledProcessError, ValueError):
        return None


def find_script(name: str = SCRIPT_NAME) -> Path:
    """Locate a helper script: $TELLYBOX_SCRIPTS_DIR, the source checkout, or ./scripts (the image's /app)."""
    candidates = []
    if env := os.environ.get("TELLYBOX_SCRIPTS_DIR"):
        candidates.append(Path(env))
    candidates += [Path(__file__).resolve().parents[1] / "scripts", Path.cwd() / "scripts"]
    for d in candidates:
        if (d / name).is_file():
            return d / name
    raise FileNotFoundError(f"{name} not found in {', '.join(map(str, candidates))}")


def cmd_migrate(args: argparse.Namespace) -> int:
    config = Config.from_env()
    conn = db.open_db(config.db_path)
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    print(f"{config.db_path}: schema version {version}")
    return 0


def cmd_make_clips(args: argparse.Namespace) -> int:
    out_dir = Path(args.out) if args.out else Config.from_env().media_dir / "dev-show"
    script = find_script()
    for n in range(1, args.count + 1):
        out = out_dir / f"ep{n:02d}.mp4"
        subprocess.run([str(script), str(out), str(args.seconds), f"EP {n}"], check=True)
    return 0


def cmd_seed(args: argparse.Namespace) -> int:
    config = Config.from_env()
    media_root = config.media_dir.resolve()
    show_dir = Path(args.dir).resolve()
    if not show_dir.is_relative_to(media_root):
        print(f"error: {show_dir} is not inside the media dir {media_root}", file=sys.stderr)
        return 2
    files = sorted(p for p in show_dir.glob("*.mp4") if p.is_file())
    if not files:
        print(f"error: no .mp4 files in {show_dir}", file=sys.stderr)
        return 2

    now = datetime.now(UTC)
    conn = db.open_db(config.db_path)
    conn.execute("BEGIN")
    try:
        show_id = library.create_show(conn, args.show or show_dir.name, now=now, autoplay=args.autoplay)
        added = []
        for f in files:
            duration = probe_duration(f)
            rel = f.relative_to(media_root).as_posix()
            added.append((library.add_episode(conn, show_id, f.stem, rel, now=now, duration_s=duration), rel, duration))
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise

    print(f"show {show_id}: {args.show or show_dir.name}")
    for episode_id, rel, duration in added:
        shown = f"{duration:.1f}s" if duration is not None else "unknown duration"
        print(f"  episode {episode_id}: {rel} ({shown})")
    return 0


def _ytdlp(config: Config) -> YtDlp:
    return YtDlp(config.data_dir / "tools" / "yt-dlp")


def _duration(seconds: float | None) -> str:
    if seconds is None:
        return "?"
    m, s = divmod(int(seconds), 60)
    return f"{m // 60}:{m % 60:02d}:{s:02d}" if m >= 60 else f"{m}:{s:02d}"


def cmd_add(args: argparse.Namespace) -> int:
    config = Config.from_env()
    try:
        info = ingest.preview(_ytdlp(config), args.url)
    except YtDlpError as exc:
        print(f"error: {exc.message}", file=sys.stderr)
        return 1
    print(f"title:    {info.title}")
    print(f"channel:  {info.channel_name} ({info.channel_id})")
    print(f"duration: {_duration(info.duration_s)}")
    print(f"thumb:    {info.thumbnail_url}")
    if info.chapters:
        print(f"chapters: {len(info.chapters)}")
        for c in info.chapters:
            print(f"  {_duration(c.start_s):>8}  {c.title}")
    print(f"publish:  {'hold (hidden until published or split)' if args.hold else 'when ready'}")
    if not args.yes and input("add this video? [y/N] ").strip().lower() not in ("y", "yes"):
        print("not added")
        return 1
    conn = db.open_db(config.db_path)
    try:
        source_id, job_id = ingest.add(conn, info, publish=not args.hold, now=datetime.now(UTC))
    except ingest.AlreadyAdded as exc:
        print(f"already added as source video {exc.source_video_id} ({exc.status})", file=sys.stderr)
        return 1
    print(f"queued: source video {source_id}, job {job_id}")
    return 0


def cmd_jobs(args: argparse.Namespace) -> int:
    conn = db.open_db(Config.from_env().db_path)
    for j in jobs.list_jobs(conn, limit=args.limit):
        title = ""
        if j.target_id is not None and (src := ingest.get_source_video(conn, j.target_id)):
            title = src.title
        progress = f"{j.progress * 100:3.0f}%" if j.progress is not None and j.status not in ("ready", "failed") else ""
        line = f"{j.id:>4}  {j.type:<12} {j.status:<11} {progress:>4}  attempt {j.attempts}/{j.max_attempts}  {title}"
        print(line.rstrip())
        if j.error and j.status != "ready":
            print(f"      error: {j.error}")
    return 0


def cmd_retry(args: argparse.Namespace) -> int:
    conn = db.open_db(Config.from_env().db_path)
    try:
        job = ingest.retry(conn, args.job_id, now=datetime.now(UTC))
    except (KeyError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"job {job.id} queued again")
    return 0


def cmd_ytdlp_update(args: argparse.Namespace) -> int:
    conn = db.open_db(Config.from_env().db_path)
    job_id = ingest.request_ytdlp_update(conn, now=datetime.now(UTC))
    print(f"queued update job {job_id}" if job_id else "an update is already pending")
    return 0


def cmd_backup(args: argparse.Namespace) -> int:
    config = Config.from_env()
    try:
        path = backup.backup(config.db_path, Path(args.dest), datetime.now(UTC), config.tz, keep=args.keep)
    except backup.BackupError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"backup: {path.name} ({path.stat().st_size} bytes)")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tellybox")
    sub = parser.add_subparsers(dest="command")

    p = sub.add_parser("migrate", help="apply database migrations")
    p.set_defaults(func=cmd_migrate)

    p = sub.add_parser("add", help="preview a YouTube video and queue its download")
    p.add_argument("url")
    p.add_argument("--hold", action="store_true", help="keep the episode hidden when ready (e.g. to split it first)")
    p.add_argument("-y", "--yes", action="store_true", help="don't ask for confirmation")
    p.set_defaults(func=cmd_add)

    p = sub.add_parser("jobs", help="list background jobs")
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(func=cmd_jobs)

    p = sub.add_parser("retry", help="retry a failed job")
    p.add_argument("job_id", type=int)
    p.set_defaults(func=cmd_retry)

    p = sub.add_parser("ytdlp-update", help="queue a yt-dlp update")
    p.set_defaults(func=cmd_ytdlp_update)

    p = sub.add_parser("backup", help="write a consistent database backup")
    p.add_argument("dest", metavar="DEST")
    p.add_argument("--keep", type=int, default=14, help="how many backups to keep (default: 14)")
    p.set_defaults(func=cmd_backup)

    dev = sub.add_parser("dev", help="developer helpers")
    dev_sub = dev.add_subparsers(dest="dev_command")

    p = dev_sub.add_parser("seed", help="register every *.mp4 in DIR as episodes of a new show")
    p.add_argument("dir", metavar="DIR", help="directory inside the media dir")
    p.add_argument("--show", help="show name (default: DIR's basename)")
    p.add_argument("--no-autoplay", dest="autoplay", action="store_false", help="turn off autoplay (PB-3)")
    p.set_defaults(func=cmd_seed)

    p = dev_sub.add_parser("make-clips", help="generate N labelled test episodes ep01.mp4...")
    p.add_argument("count", metavar="N", type=int)
    p.add_argument("--seconds", type=int, default=120)
    p.add_argument("--out", metavar="DIR", help="output directory (default: <media_dir>/dev-show)")
    p.set_defaults(func=cmd_make_clips)
    return parser


def main(argv: list[str] | None = None) -> int:
    # `docker compose exec` starts as root: write files as the service user (DP-3).
    if os.geteuid() == 0:
        try:
            privileges.drop_root(*privileges.target_ids())
        except ValueError as e:
            print(f"tellybox: {e}", file=sys.stderr)
            return 2
    logging.basicConfig(level=logging.INFO, format="%(asctime)s level=%(levelname)s logger=%(name)s %(message)s")
    parser = build_parser()
    args = parser.parse_args(argv)
    if not hasattr(args, "func"):
        parser.print_help(sys.stderr)
        return 2
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
