"""Add a playlist (/admin/add, CI-7): URL kinds, the playlist preview, and adding a selection."""

from __future__ import annotations

import re

import pytest

from tellybox import ingest
from tellybox.ytdlp import PlaylistEntry, PlaylistInfo, VideoInfo, YtDlpError

PLAYLIST_URL = "https://www.youtube.com/playlist?list=PLkids123"
BOTH_URL = "https://www.youtube.com/watch?v=vid0001&list=PLkids123"
VIDEO_URL = "https://www.youtube.com/watch?v=vid0001"
CHANNEL_URL = "https://www.youtube.com/@KidsChannel"


def entry(youtube_id: str, title: str | None = None, *, unavailable: str | None = None) -> PlaylistEntry:
    return PlaylistEntry(
        youtube_id=youtube_id, url=f"https://www.youtube.com/watch?v={youtube_id}",
        title=title or f"Song {youtube_id}", channel_id="UCkids", channel_name="Kids Channel",
        duration_s=185.0, thumbnail_url=f"https://i.ytimg.com/vi/{youtube_id}/hq.jpg",
        unavailable_reason=unavailable,
    )


def playlist(entries=None, *, total_count=None, title="Nursery Rhymes") -> PlaylistInfo:
    entries = entries if entries is not None else [entry("vid0001"), entry("vid0002"), entry("vid0003")]
    return PlaylistInfo(
        playlist_id="PLkids123", url=PLAYLIST_URL, title=title, channel_name="Kids Channel",
        total_count=total_count if total_count is not None else len(entries), entries=entries,
    )


class FakeYtDlp:
    def __init__(self) -> None:
        self.playlist = playlist()
        self.info = VideoInfo(
            youtube_id="vid0001", url=VIDEO_URL, title="Just one video", channel_id="UCkids",
            channel_name="Kids Channel", duration_s=125.0, thumbnail_url=None, chapters=[], is_live=False,
        )
        self.error: YtDlpError | None = None
        self.calls: list[tuple[str, str]] = []

    def preview(self, url: str) -> VideoInfo:
        self.calls.append(("video", url))
        if self.error:
            raise self.error
        return self.info

    def preview_playlist(self, url: str) -> PlaylistInfo:
        self.calls.append(("playlist", url))
        if self.error:
            raise self.error
        return self.playlist


@pytest.fixture
def ytdlp():
    return FakeYtDlp()


def _preview(admin, url: str = PLAYLIST_URL, **extra):
    return admin.post("/admin/add/preview", data={"url": url, **extra})


def _preview_id(text: str) -> str:
    return re.search(r'name="preview_id" value="([^"]+)"', text).group(1)


def _checkbox(text: str, youtube_id: str) -> str:
    return re.search(rf'<input type="checkbox" name="video" value="{youtube_id}"[^>]*>', text).group(0)


def _sources(conn):
    return conn.execute(
        "SELECT youtube_id, publish, status, playlist_id, playlist_title FROM source_video ORDER BY id"
    ).fetchall()


def _add_existing(admin_env, youtube_id: str) -> None:
    info = VideoInfo(youtube_id=youtube_id, url=f"https://www.youtube.com/watch?v={youtube_id}", title="Old",
                     channel_id=None, channel_name=None, duration_s=10.0, thumbnail_url=None, chapters=[],
                     is_live=False)
    ingest.add(admin_env.conn, info, publish=True, now=admin_env.clock.now())


# --------------------------------------------------------------------------- URL kinds


def test_channel_url_links_to_subscriptions(admin, ytdlp):
    r = _preview(admin, CHANNEL_URL)
    assert r.status_code == 200
    assert 'href="/admin/subscriptions?url=https%3A//www.youtube.com/%40KidsChannel"' in r.text
    assert ytdlp.calls == []


def test_video_in_playlist_asks_which(admin, ytdlp):
    r = _preview(admin, BOTH_URL)
    assert r.status_code == 200
    assert "Just this video" in r.text and "Whole playlist" in r.text
    assert 'name="mode" value="video"' in r.text and 'name="mode" value="playlist"' in r.text
    assert ytdlp.calls == []


def test_both_with_mode_video_uses_single_video_flow(admin, ytdlp):
    r = _preview(admin, BOTH_URL, mode="video")
    assert r.status_code == 200
    assert ytdlp.calls == [("video", BOTH_URL)]
    assert "Just one video" in r.text
    assert "Add and hold" in r.text
    assert 'action="/admin/add"' in r.text


def test_both_with_mode_playlist_shows_playlist(admin, ytdlp):
    r = _preview(admin, BOTH_URL, mode="playlist")
    assert r.status_code == 200
    assert ytdlp.calls == [("playlist", BOTH_URL)]
    assert "Nursery Rhymes" in r.text


def test_mode_does_not_override_a_plain_video_url(admin, ytdlp):
    r = _preview(admin, VIDEO_URL, mode="playlist")
    assert r.status_code == 200
    assert ytdlp.calls == [("video", VIDEO_URL)]


def test_playlist_preview_error_is_shown(admin, ytdlp):
    ytdlp.error = YtDlpError("This playlist does not exist", retryable=False)
    r = _preview(admin)
    assert r.status_code == 422
    assert "This playlist does not exist" in r.text


# --------------------------------------------------------------------------- playlist preview


def test_playlist_preview_lists_videos_ticked(admin, ytdlp):
    r = _preview(admin)
    assert r.status_code == 200
    assert "Nursery Rhymes" in r.text
    assert "3 videos" in r.text
    for vid in ("vid0001", "vid0002", "vid0003"):
        box = _checkbox(r.text, vid)
        assert "checked" in box and "disabled" not in box
        assert f"https://i.ytimg.com/vi/{vid}/hq.jpg" in r.text
    assert 'referrerpolicy="no-referrer"' in r.text and 'loading="lazy"' in r.text
    assert "3:05" in r.text  # duration
    assert re.search(r'name="hold" value="1" checked', r.text)
    assert re.search(r"Add 3 videos\s*</button>", r.text)


def test_playlist_preview_greys_out_added_and_unavailable(admin, admin_env, ytdlp):
    _add_existing(admin_env, "vid0001")
    ytdlp.playlist = playlist([entry("vid0001"), entry("vid0002", unavailable="private"), entry("vid0003")])
    r = _preview(admin)
    assert "Already added" in r.text
    assert "Private" in r.text
    for vid in ("vid0001", "vid0002"):
        box = _checkbox(r.text, vid)
        assert "disabled" in box and "checked" not in box
    assert "checked" in _checkbox(r.text, "vid0003")
    assert re.search(r"Add 1 video\s*</button>", r.text)


def test_playlist_preview_mentions_cap(admin, ytdlp):
    ytdlp.playlist = playlist([entry(f"v{i:06d}") for i in range(200)], total_count=250)
    r = _preview(admin)
    assert "showing the first 200 of 250" in r.text


def test_playlist_preview_escapes_titles(admin, ytdlp):
    evil = '<script>alert("x")</script>'
    ytdlp.playlist = playlist([entry("vid0001", evil)], title=f"<img src=x onerror=alert(1)>")
    r = _preview(admin)
    assert "<script>alert" not in r.text
    assert "<img src=x" not in r.text
    assert "&lt;script&gt;" in r.text
    assert "&lt;img src=x" in r.text


def test_playlist_with_nothing_addable_disables_submit(admin, admin_env, ytdlp):
    ytdlp.playlist = playlist([entry("vid0001", unavailable="deleted")])
    r = _preview(admin)
    assert re.search(r'id="playlist-submit"\s+disabled', r.text)


# --------------------------------------------------------------------------- adding


def _add(admin, pid: str, videos, hold: bool = True):
    data = {"preview_id": pid, "video": list(videos)}
    if hold:
        data["hold"] = "1"
    return admin.post("/admin/add/playlist", data=data, follow_redirects=False)


def test_add_playlist_holds_selected_videos_by_default(admin, admin_env, ytdlp):
    pid = _preview_id(_preview(admin).text)
    r = _add(admin, pid, ["vid0001", "vid0003"])
    assert r.status_code == 303
    assert r.headers["location"] == "/admin/jobs"
    rows = _sources(admin_env.conn)
    assert [(s["youtube_id"], s["publish"], s["playlist_id"], s["playlist_title"]) for s in rows] == [
        ("vid0001", "hold", "PLkids123", "Nursery Rhymes"),
        ("vid0003", "hold", "PLkids123", "Nursery Rhymes"),
    ]
    jobs = admin_env.conn.execute("SELECT COUNT(*) FROM job WHERE type = 'download'").fetchone()[0]
    assert jobs == 2
    page = admin.get("/admin/jobs")
    assert "Added 2 videos (held)" in page.text
    assert "skipped" not in page.text


def test_add_playlist_unticked_hold_publishes(admin, admin_env, ytdlp):
    pid = _preview_id(_preview(admin).text)
    _add(admin, pid, ["vid0002"], hold=False)
    rows = _sources(admin_env.conn)
    assert [(s["youtube_id"], s["publish"]) for s in rows] == [("vid0002", "publish")]
    page = admin.get("/admin/jobs").text
    assert "Added 1 video" in page and "(held)" not in page


def test_add_playlist_flash_reports_skipped(admin, admin_env, ytdlp):
    pid = _preview_id(_preview(admin).text)
    _add_existing(admin_env, "vid0002")  # added elsewhere after the preview was fetched
    r = _add(admin, pid, ["vid0001", "vid0002", "vid0003"])
    assert r.status_code == 303
    assert "Added 2 videos (held), skipped 1" in admin.get("/admin/jobs").text


def test_add_playlist_rejects_ids_outside_the_preview(admin, admin_env, ytdlp):
    pid = _preview_id(_preview(admin).text)
    r = _add(admin, pid, ["vid0001", "evil999"])
    assert r.status_code == 422
    assert _sources(admin_env.conn) == []


def test_add_playlist_requires_a_selection(admin, admin_env, ytdlp):
    pid = _preview_id(_preview(admin).text)
    r = _add(admin, pid, [])
    assert r.status_code == 422
    assert "Tick at least one video" in r.text
    assert "Nursery Rhymes" in r.text  # the preview is shown again
    assert _sources(admin_env.conn) == []


def test_add_playlist_expired_preview(admin, admin_env, ytdlp):
    r = _add(admin, "does-not-exist", ["vid0001"])
    assert r.status_code == 422
    assert "expired" in r.text.lower()
    pid = _preview_id(_preview(admin).text)
    admin_env.clock.advance(seconds=31 * 60)
    r = _add(admin, pid, ["vid0001"])
    assert r.status_code == 422
    assert "expired" in r.text.lower()
    assert _sources(admin_env.conn) == []


def test_add_playlist_uses_preview_only_once(admin, ytdlp):
    pid = _preview_id(_preview(admin).text)
    assert _add(admin, pid, ["vid0001"]).status_code == 303
    assert _add(admin, pid, ["vid0002"]).status_code == 422


def test_single_video_and_playlist_previews_are_not_interchangeable(admin, admin_env, ytdlp):
    playlist_pid = _preview_id(_preview(admin).text)
    r = admin.post("/admin/add", data={"preview_id": playlist_pid, "action": "add"})
    assert r.status_code == 422
    video_pid = _preview_id(_preview(admin, VIDEO_URL).text)
    assert _add(admin, video_pid, ["vid0001"]).status_code == 422
    assert _sources(admin_env.conn) == []


def test_add_playlist_refuses_cross_origin(admin_env, ytdlp):
    from fastapi.testclient import TestClient

    from tests.web.admin.conftest import sign_in

    client = TestClient(admin_env.app, headers={"Origin": "http://testserver"})
    sign_in(client)
    pid = _preview_id(client.post("/admin/add/preview", data={"url": PLAYLIST_URL}).text)
    r = client.post("/admin/add/playlist", data={"preview_id": pid, "video": ["vid0001"]},
                    headers={"Origin": "http://evil.example"}, follow_redirects=False)
    assert r.status_code == 403
    assert _sources(admin_env.conn) == []
