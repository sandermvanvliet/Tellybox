"""yt-dlp subprocess wrapper (CI-1, CI-2, CI-5), exercised against a fake `yt_dlp` package."""

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tellybox import ytdlp
from tellybox.sponsorblock import Segment
from tellybox.ytdlp import Chapter, SponsorBlockUnavailable, YtDlp, YtDlpError, classify_error, update

FIXTURES = Path(__file__).parent / "fixtures" / "ytdlp"


def make_fake(target: Path, version: str = "2026.01.01") -> None:
    pkg = target / "yt_dlp"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text(f"__version__ = {version!r}\n")
    shutil.copy(FIXTURES / "fake_main.py", pkg / "__main__.py")
    shutil.copy(FIXTURES / "video.json", pkg / "video.json")
    shutil.copy(FIXTURES / "playlist_flat.json", pkg / "playlist_flat.json")
    (target / "bin").mkdir()


def installer(version: str):
    return lambda target: make_fake(target, version)


def activate(tools: Path, name: str = "v1", version: str = "2026.01.01") -> Path:
    make_fake(tools / "versions" / name, version)
    (tools / "current").write_text(name + "\n")
    return tools / "versions" / name


@pytest.fixture
def tools(tmp_path):
    return tmp_path / "tools"


@pytest.fixture
def yt(tools):
    activate(tools)
    return YtDlp(tools)


# --- command / env -----------------------------------------------------------


def test_fallback_without_active_dir(tools):
    y = YtDlp(tools, python="/usr/bin/python3")
    assert y.active_dir() is None
    assert y.command() == ["/usr/bin/python3", "-m", "yt_dlp"]
    env = y.env()
    assert env.get("PYTHONPATH") == os.environ.get("PYTHONPATH")
    assert env["PATH"].split(os.pathsep)[-1] == "/usr/bin"  # fallback deno next to the interpreter


def test_current_pointing_to_missing_dir_falls_back(tools):
    tools.mkdir()
    (tools / "current").write_text("gone\n")
    assert YtDlp(tools).active_dir() is None


def test_env_with_active_dir(tools, monkeypatch):
    monkeypatch.setenv("PYTHONPATH", "/other")
    active = activate(tools)
    y = YtDlp(tools)
    assert y.active_dir() == active
    env = y.env()
    assert env["PYTHONPATH"].split(os.pathsep) == [str(active), "/other"]
    assert env["PATH"].split(os.pathsep)[0] == str(active / "bin")


def test_version_runs_active_copy(yt):
    assert yt.version() == "2026.01.01"


# --- preview (CI-1) ----------------------------------------------------------


def test_preview_parses_info(yt):
    info = yt.preview("https://youtu.be/abc")
    assert info.youtube_id == "abc123XYZ_-"
    assert info.url == "https://www.youtube.com/watch?v=abc123XYZ_-"
    assert info.title == "Tractor Tom - Compilation"
    assert (info.channel_id, info.channel_name) == ("UCtractor", "Tractor Tom Official")
    assert info.duration_s == 1234.5
    assert info.thumbnail_url.endswith("maxresdefault.jpg")
    assert info.chapters == [Chapter(0.0, 600.0, "Episode 1"), Chapter(600.0, 1234.5, "Episode 2")]
    assert info.is_live is False


def test_preview_channel_falls_back_to_uploader(yt):
    assert yt.preview("https://youtu.be/nochannel").channel_name == "tractortom"


@pytest.mark.parametrize("url", ["https://youtu.be/live", "https://youtube.com/playlist?list=PL1"])
def test_preview_rejects_live_and_playlists(yt, url):
    with pytest.raises(YtDlpError) as e:
        yt.preview(url)
    assert e.value.retryable is False


def test_preview_error_uses_last_error_line(yt):
    with pytest.raises(YtDlpError) as e:
        yt.preview("https://youtu.be/unavailable")
    assert e.value.message == "[youtube] abc: Video unavailable"
    assert e.value.retryable is False


def test_parse_info_requires_id():
    with pytest.raises(YtDlpError) as e:
        ytdlp.parse_info({"title": "x"})
    assert not e.value.retryable


# --- playlists (CI-7) --------------------------------------------------------


@pytest.mark.parametrize("url, kind", [
    ("https://www.youtube.com/watch?v=abc123XYZ_-", "video"),
    ("https://www.youtube.com/watch?v=abc123XYZ_-&list=PLtractor123", "both"),
    ("https://www.youtube.com/watch?list=PLtractor123&v=abc123XYZ_-&index=3", "both"),
    ("https://m.youtube.com/watch?v=abc123XYZ_-&list=PLtractor123", "both"),
    ("https://www.youtube.com/watch?v=abc123XYZ_-&list=RDabc123XYZ_-&start_radio=1", "video"),  # Mix
    ("https://www.youtube.com/watch?v=abc123XYZ_-&list=RDMMabc123XYZ_-", "video"),  # My Mix
    ("https://www.youtube.com/playlist?list=PLtractor123", "playlist"),
    ("https://youtube.com/playlist?list=PLtractor123&si=tracking", "playlist"),
    ("www.youtube.com/playlist?list=PLtractor123", "playlist"),  # pasted without a scheme
    ("https://www.youtube.com/watch?list=PLtractor123", "playlist"),
    ("https://youtu.be/abc123XYZ_-", "video"),
    ("https://youtu.be/abc123XYZ_-?si=x", "video"),
    ("https://youtu.be/abc123XYZ_-?list=PLtractor123", "both"),
    ("https://www.youtube.com/@tractortom", "channel"),
    ("https://www.youtube.com/@tractortom/videos", "channel"),
    ("https://www.youtube.com/channel/UCtractor", "channel"),
    ("https://www.youtube.com/c/TractorTom", "channel"),
    ("https://www.youtube.com/user/tractortom", "channel"),
    ("https://www.youtube.com/shorts/abc123XYZ_-", "video"),
    ("https://music.youtube.com/watch?v=abc123XYZ_-", "video"),
    ("https://music.youtube.com/watch?v=abc123XYZ_-&list=OLAK5uy_album", "both"),
    ("https://music.youtube.com/playlist?list=OLAK5uy_album", "playlist"),
    ("https://music.youtube.com/channel/UCtractor", "channel"),
    ("https://vimeo.com/123456", "video"),
    ("https://example.com/playlist?list=PL1", "video"),  # only YouTube URLs are classified
    ("https://example.com/@someone", "video"),
    ("not a url", "video"),
])
def test_classify_url(url, kind):
    assert ytdlp.classify_url(url) == kind


def playlist_fixture() -> dict:
    return json.loads((FIXTURES / "playlist_flat.json").read_text())


def test_parse_playlist():
    pl = ytdlp.parse_playlist(playlist_fixture())
    assert pl.playlist_id == "PLtractor123"
    assert pl.url == "https://www.youtube.com/playlist?list=PLtractor123"
    assert (pl.title, pl.channel_name, pl.total_count) == ("Tractor Tom - Full Episodes", "Tractor Tom Official", 7)
    assert [e.youtube_id for e in pl.entries] == [f"vid0000000{i}" for i in range(1, 8)]  # playlist order
    first, second = pl.entries[:2]
    assert first == ytdlp.PlaylistEntry(
        youtube_id="vid00000001", url="https://www.youtube.com/watch?v=vid00000001",
        title="Tractor Tom - Episode 1", channel_id="UCtractor", channel_name="Tractor Tom Official",
        duration_s=661.0, thumbnail_url="https://i.ytimg.com/vi/vid00000001/hqdefault.jpg?sqp=large",  # largest
        unavailable_reason=None,
    )
    assert second.url == "https://www.youtube.com/watch?v=vid00000002"  # never carries the list
    assert second.thumbnail_url == "https://i.ytimg.com/vi/vid00000002/hqdefault.jpg"  # fallback
    assert (second.channel_id, second.duration_s, second.unavailable_reason) == ("UCpeppa", 300.5, None)  # unlisted is fine
    assert [e.unavailable_reason for e in pl.entries[2:]] == ["private", "deleted", "upcoming", "unavailable", "live"]


@pytest.mark.parametrize("availability, reason", [
    (None, None), ("public", None), ("unlisted", None), ("private", "private"),
    ("needs_auth", "unavailable"), ("subscriber_only", "unavailable"), ("premium_only", "unavailable"),
])
def test_parse_playlist_availability(availability, reason):
    data = playlist_fixture()
    data["entries"] = [dict(data["entries"][0], availability=availability)]
    assert ytdlp.parse_playlist(data).entries[0].unavailable_reason == reason


def test_parse_playlist_caps_entries():
    data = playlist_fixture()
    template = data["entries"][0]
    data["entries"] = [dict(template, id=f"v{i:010d}") for i in range(ytdlp.PLAYLIST_CAP + 50)]
    data["playlist_count"] = ytdlp.PLAYLIST_CAP + 50
    pl = ytdlp.parse_playlist(data)
    assert len(pl.entries) == ytdlp.PLAYLIST_CAP
    assert pl.entries[-1].youtube_id == f"v{ytdlp.PLAYLIST_CAP - 1:010d}"
    assert pl.total_count == ytdlp.PLAYLIST_CAP + 50


def test_parse_playlist_skips_entries_without_id():
    data = playlist_fixture()
    data["entries"] = [None, {"title": "no id"}, data["entries"][0]]
    assert [e.youtube_id for e in ytdlp.parse_playlist(data).entries] == ["vid00000001"]


@pytest.mark.parametrize("data", [
    {"id": "abc123XYZ_-", "title": "a single video"},
    {"_type": "url", "id": "abc", "url": "https://www.youtube.com/watch?v=abc"},
    {"_type": "playlist", "entries": []},  # no id
    {"_type": "playlist", "id": "UCtractor", "channel_id": "UCtractor", "entries": []},  # a channel page
])
def test_parse_playlist_rejects_non_playlists(data):
    with pytest.raises(YtDlpError) as e:
        ytdlp.parse_playlist(data)
    assert e.value.retryable is False


@pytest.mark.parametrize("url", [
    "https://www.youtube.com/playlist?list=PLtractor123",
    "https://www.youtube.com/watch?v=abc123XYZ_-&list=PLtractor123&index=4",  # "both": still the playlist
    "https://music.youtube.com/playlist?list=PLtractor123",
])
def test_preview_playlist_command(yt, monkeypatch, url):
    calls = []

    def run(args, timeout):
        calls.append((args, timeout))
        return json.dumps(playlist_fixture())

    monkeypatch.setattr(yt, "_run", run)
    pl = yt.preview_playlist(url)
    assert pl.playlist_id == "PLtractor123"
    (args, timeout), = calls
    assert args == ["-J", "--flat-playlist", "--skip-download", "--playlist-end", str(ytdlp.PLAYLIST_CAP),
                    "--", "https://www.youtube.com/playlist?list=PLtractor123"]
    assert timeout == 120


def test_preview_playlist_runs_ytdlp(yt):  # through the (fake) subprocess
    pl = yt.preview_playlist("https://www.youtube.com/playlist?list=PLtractor123")
    assert pl.title == "Tractor Tom - Full Episodes"
    assert len(pl.entries) == 7


@pytest.mark.parametrize("url", ["https://www.youtube.com/@tractortom", "https://www.youtube.com/channel/UCtractor"])
def test_preview_playlist_rejects_channels_without_running(yt, monkeypatch, url):
    monkeypatch.setattr(yt, "_run", lambda *a: pytest.fail("yt-dlp should not run"))
    with pytest.raises(YtDlpError) as e:
        yt.preview_playlist(url)
    assert e.value.retryable is False
    assert "channel" in e.value.message.lower()


def test_preview_playlist_rejects_single_video(yt):
    with pytest.raises(YtDlpError) as e:
        yt.preview_playlist("https://www.youtube.com/watch?v=abc123XYZ_-")
    assert e.value.retryable is False


def test_preview_playlist_invalid_json_is_retryable(yt, monkeypatch):
    monkeypatch.setattr(yt, "_run", lambda args, timeout: "not json")
    with pytest.raises(YtDlpError) as e:
        yt.preview_playlist("https://www.youtube.com/playlist?list=PL1")
    assert e.value.retryable is True


# --- download (CI-2) ---------------------------------------------------------


def test_download_result_and_progress(yt, tmp_path):
    seen: list[float] = []
    res = yt.download("https://youtu.be/abc", tmp_path / "dl", on_progress=seen.append)
    dest = (tmp_path / "dl").resolve()
    assert res.video_path == dest / "video.mp4"
    assert res.video_path.read_bytes() == b"fake mp4"
    assert res.thumbnail_path == dest / "thumb.jpg"
    assert res.info.youtube_id == "abc123XYZ_-"
    assert len(res.info.chapters) == 2
    # Two files (136+140), each 0..1, combined; monotonic, throttled to >= 1% steps, ends at 1.
    assert seen[0] == 0.0 and seen[-1] == 1.0
    assert all(b - a >= 0.01 - 1e-9 for a, b in zip(seen, seen[1:]))
    assert any(0.45 <= p <= 0.55 for p in seen)  # first file done ~ halfway
    assert len(seen) <= 101


def test_download_without_thumbnail(yt, tmp_path):
    assert yt.download("https://youtu.be/nothumb", tmp_path).thumbnail_path is None


@pytest.mark.parametrize(
    ("url", "message", "retryable"),
    [
        ("https://youtu.be/unavailable", "[youtube] abc: Video unavailable", False),
        ("https://youtu.be/flaky", "unable to download video data: HTTP Error 503: Service Unavailable", True),
    ],
)
def test_download_errors(yt, tmp_path, url, message, retryable):
    with pytest.raises(YtDlpError) as e:
        yt.download(url, tmp_path)
    assert (e.value.message, e.value.retryable) == (message, retryable)


# --- SponsorBlock (SB-1, SB-3, SB-5) --------------------------------------------

SB = ["sponsor", "selfpromo", "interaction"]


def test_download_with_sponsorblock_reports_removed_segments(yt, tmp_path):
    res = yt.download("https://youtu.be/sponsored", tmp_path, sb_categories=SB)
    assert res.sponsor_segments == [Segment("sponsor", 30.0, 70.0)]  # overlap merged; poi and intro left out
    assert len(res.info.chapters) == 2  # the video's own chapters stay


def test_download_with_sponsorblock_drops_made_up_chapter(yt, tmp_path):
    res = yt.download("https://youtu.be/sponsored-nochapters", tmp_path, sb_categories=SB)
    assert res.sponsor_segments and res.info.chapters == []


def test_download_without_sponsorblock_or_segments(yt, tmp_path):
    assert yt.download("https://youtu.be/sponsored", tmp_path / "a").sponsor_segments == []
    res = yt.download("https://youtu.be/plain", tmp_path / "b", sb_categories=SB)
    assert res.sponsor_segments == [] and len(res.info.chapters) == 2


def test_download_sponsorblock_unreachable(yt, tmp_path):
    with pytest.raises(SponsorBlockUnavailable) as e:
        yt.download("https://youtu.be/sbdown", tmp_path, sb_categories=SB)
    assert e.value.retryable and "SponsorBlock" in e.value.message
    assert yt.download("https://youtu.be/sbdown", tmp_path / "b").sponsor_segments == []  # SB-5 fallback


def test_sponsor_segments_recheck(yt):
    assert yt.sponsor_segments("https://youtu.be/sponsored", SB) == [Segment("sponsor", 30.0, 70.0)]
    assert yt.sponsor_segments("https://youtu.be/sponsored", ["intro"]) == [Segment("intro", 700.0, 710.0)]
    assert yt.sponsor_segments("https://youtu.be/plain", SB) == []
    assert yt.sponsor_segments("https://youtu.be/sponsored", []) == []
    with pytest.raises(SponsorBlockUnavailable):
        yt.sponsor_segments("https://youtu.be/sbdown", SB)
    with pytest.raises(YtDlpError) as e:
        yt.sponsor_segments("https://youtu.be/unavailable", SB)
    assert not isinstance(e.value, SponsorBlockUnavailable) and not e.value.retryable


@pytest.mark.parametrize("url", ["https://youtu.be/slow", "https://youtu.be/stall"])
def test_download_timeout_kills_process(yt, tmp_path, url):
    start = time.monotonic()
    with pytest.raises(YtDlpError) as e:
        yt.download(url, tmp_path, timeout=1.5)
    assert time.monotonic() - start < 10
    assert e.value.retryable and "timed out" in e.value.message


def test_preview_timeout(yt):
    with pytest.raises(YtDlpError) as e:
        yt.preview("https://youtu.be/slow", timeout=1)
    assert e.value.retryable


# --- error classification ----------------------------------------------------


@pytest.mark.parametrize(
    ("stderr", "retryable"),
    [
        ("ERROR: [youtube] x: Video unavailable", False),
        ("ERROR: [youtube] x: Private video. Sign in if you've been granted access", False),
        ("ERROR: [youtube] x: This video has been removed by the uploader", False),
        ("ERROR: [youtube] x: This video is available to this channel's members-only", False),
        ("ERROR: [youtube] x: Join this channel to get access to members-only content", False),
        ("ERROR: [youtube] x: Sign in to confirm your age. This video may be inappropriate", False),
        ("ERROR: Unsupported URL: https://example.com/", False),
        ("ERROR: 'foo' is not a valid URL", False),
        ("ERROR: [youtube] x: Premieres in 3 hours", False),
        ("ERROR: [youtube] x: This live event will begin in 5 minutes", False),
        ("ERROR: unable to download video data: HTTP Error 503: Service Unavailable", True),
        ("ERROR: unable to download webpage: HTTP Error 429: Too Many Requests", True),
        ("ERROR: [youtube] x: Read timed out", True),
        ("ERROR: unable to download: <urlopen error [Errno 111] Connection refused>", True),
        ("ERROR: [youtube] x: Sign in to confirm you’re not a bot", True),
        ("ERROR: [youtube] x: Sign in to confirm you're not a bot", True),
        ("ERROR: something nobody has seen before", True),
    ],
)
def test_classify_error(stderr, retryable):
    assert classify_error(stderr) is retryable


# --- update (CI-5) -----------------------------------------------------------


def versions(tools: Path) -> set[str]:
    return {d.name for d in (tools / "versions").iterdir()}


def test_update_first_install_switches(tools):
    res = update(tools, installer=installer("2026.08.19"))
    assert (res.version, res.previous, res.changed) == ("2026.08.19", None, True)
    assert YtDlp(tools).active_dir() == res.path
    assert YtDlp(tools).version() == "2026.08.19"
    assert not list(tools.glob("current.*"))  # no tmp file left behind


def test_update_switches_and_keeps_previous(tools):
    old = activate(tools, "old", "2026.01.01")
    res = update(tools, installer=installer("2026.08.19"))
    assert (res.version, res.previous, res.changed) == ("2026.08.19", "2026.01.01", True)
    assert YtDlp(tools).version() == "2026.08.19"
    assert versions(tools) == {old.name, res.path.name}


def test_update_same_version_is_noop(tools):
    old = activate(tools, "old", "2026.08.19")
    res = update(tools, installer=installer("2026.08.19"))
    assert (res.version, res.previous, res.changed, res.path) == ("2026.08.19", "2026.08.19", False, old)
    assert versions(tools) == {"old"}
    assert YtDlp(tools).active_dir() == old


@pytest.mark.parametrize(
    "bad_installer",
    [installer("broken"), lambda target: None, lambda target: (_ for _ in ()).throw(OSError("disk full"))],
    ids=["version-fails", "nothing-installed", "installer-raises"],
)
def test_update_failed_verification_keeps_old(tools, bad_installer):
    old = activate(tools, "old", "2026.01.01")
    with pytest.raises(YtDlpError) as e:
        update(tools, installer=bad_installer)
    assert e.value.retryable
    assert YtDlp(tools).active_dir() == old
    assert versions(tools) == {"old"}


def test_update_prunes_to_two(tools):
    activate(tools, "ancient", "2025.01.01")
    first = update(tools, installer=installer("2026.01.01"))
    time.sleep(0.001)
    second = update(tools, installer=installer("2026.08.19"))
    assert versions(tools) == {first.path.name, second.path.name}
    assert YtDlp(tools).active_dir() == second.path


def test_default_installer_uses_pip_target(tools, monkeypatch):
    real_run, pips = subprocess.run, []

    def run(cmd, **kw):
        if "pip" not in cmd:
            return real_run(cmd, **kw)
        pips.append(cmd)
        make_fake(Path(cmd[cmd.index("--target") + 1]), "2026.08.19")  # no network
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(ytdlp.subprocess, "run", run)
    assert update(tools).version == "2026.08.19"
    [pip] = pips
    assert pip[:4] == [sys.executable, "-m", "pip", "install"]
    assert {"--no-cache-dir", "--upgrade"} <= set(pip) and pip[-1] == "yt-dlp[default,deno]"
