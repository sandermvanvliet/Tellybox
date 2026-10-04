"""Signed media URLs (NF-3) and recognising our media from a URL (spike finding 7)."""

from datetime import UTC, datetime, timedelta

from tellybox import media_urls
from tellybox.media_urls import MEDIA_TTL_S, episode_id_from_url, media_path, media_url, verify

SECRET = b"s3cret"
NOW = datetime(2026, 9, 28, 14, 0, tzinfo=UTC)


def split(url: str) -> tuple[int, int, str]:
    ep, exp, sig = url.rsplit("/media/", 1)[1].removesuffix(".mp4").split("/")
    return int(ep), int(exp), sig


def test_media_path_shape():
    path = media_path(SECRET, 7, 1_800_000_000)
    assert path.startswith("/media/7/1800000000/")
    assert path.endswith(".mp4")
    sig = path.rsplit("/", 1)[1].removesuffix(".mp4")
    assert len(sig) == 22
    assert "=" not in sig


def test_media_url_uses_base_and_ttl():
    url = media_url("http://192.168.1.10:8080/", SECRET, 3, NOW)
    assert url.startswith("http://192.168.1.10:8080/media/3/")
    ep, exp, sig = split(url)
    assert ep == 3
    assert exp == int(NOW.timestamp()) + MEDIA_TTL_S


def test_verify_accepts_valid_signature():
    url = media_url("http://h", SECRET, 3, NOW)
    ep, exp, sig = split(url)
    assert verify(SECRET, ep, exp, sig, NOW)
    assert verify(SECRET, ep, exp, sig, NOW + timedelta(hours=23))


def test_signature_is_stable_across_processes():
    # Deterministic given the secret: a restarted process re-derives the same URL (NF-7).
    assert media_path(SECRET, 5, 123) == media_path(SECRET, 5, 123)


def test_verify_rejects_expired():
    url = media_url("http://h", SECRET, 3, NOW, ttl_s=60)
    ep, exp, sig = split(url)
    assert not verify(SECRET, ep, exp, sig, NOW + timedelta(seconds=61))


def test_verify_rejects_tampering():
    ep, exp, sig = split(media_url("http://h", SECRET, 3, NOW))
    assert not verify(SECRET, 4, exp, sig, NOW)
    assert not verify(SECRET, ep, exp + 1, sig, NOW)
    assert not verify(b"other", ep, exp, sig, NOW)
    assert not verify(SECRET, ep, exp, sig[:-1] + ("A" if sig[-1] != "A" else "B"), NOW)
    assert not verify(SECRET, ep, exp, "", NOW)
    assert not verify(SECRET, ep, exp, "é" * 22, NOW)


def test_episode_id_from_url():
    url = media_url("http://192.168.1.10:8080", SECRET, 42, NOW)
    assert episode_id_from_url(url) == 42
    # Token-independent: any signature/expiry still identifies the episode.
    assert episode_id_from_url("http://other:1/media/42/1/abc.mp4") == 42
    assert episode_id_from_url("http://h/media/42/1/abc.mp4?x=1") == 42


def test_episode_id_from_url_rejects_foreign():
    assert episode_id_from_url(None) is None
    assert episode_id_from_url("") is None
    assert episode_id_from_url("dQw4w9WgXcQ") is None  # YouTube content id
    assert episode_id_from_url("http://h/media/x/1/abc.mp4") is None
    assert episode_id_from_url("http://h/media/1/abc.mp4") is None
    assert episode_id_from_url("http://h/other/1/2/abc.mp4") is None
    assert episode_id_from_url("http://h/media/1/2/abc.webm") is None


def test_module_exports():
    assert media_urls.MEDIA_TTL_S == 24 * 3600


# --------------------------------------------------------------------------- session-scoped URLs (PB-7, WT-11)


def test_media_path_without_a_session_is_unchanged():
    assert media_path(SECRET, 7, 1_800_000_000) == media_path(SECRET, 7, 1_800_000_000, session_id=None)
    assert "?" not in media_path(SECRET, 7, 1_800_000_000)


def test_session_path_carries_and_signs_the_session():
    path = media_path(SECRET, 7, 1_800_000_000, session_id=42)
    assert path.startswith("/media/7/1800000000/") and path.endswith(".mp4?s=42")
    base, query = path.split("?")
    sig = base.rsplit("/", 1)[1].removesuffix(".mp4")
    assert sig != media_path(SECRET, 7, 1_800_000_000).rsplit("/", 1)[1].removesuffix(".mp4")
    assert verify(SECRET, 7, 1_800_000_000, sig, NOW, session_id=42)
    assert not verify(SECRET, 7, 1_800_000_000, sig, NOW, session_id=43)  # another session
    assert not verify(SECRET, 7, 1_800_000_000, sig, NOW)  # nor does it pass as a TV URL
    assert media_urls.session_id_from_query(query) == 42


def test_a_tv_signature_does_not_open_a_session_url():
    tv_sig = media_path(SECRET, 7, 1_800_000_000).rsplit("/", 1)[1].removesuffix(".mp4")
    assert not verify(SECRET, 7, 1_800_000_000, tv_sig, NOW, session_id=42)


def test_session_url_expires_and_is_still_recognised():
    url = media_url("http://h", SECRET, 3, NOW, session_id=9)
    assert url.startswith("http://h/media/3/") and url.endswith("?s=9")
    ep, exp, sig = split(url.split("?")[0])
    assert exp == int(NOW.timestamp()) + MEDIA_TTL_S
    assert verify(SECRET, ep, exp, sig, NOW + timedelta(hours=23), session_id=9)
    assert not verify(SECRET, ep, exp, sig, NOW + timedelta(hours=25), session_id=9)
    assert episode_id_from_url(url) == 3


def test_session_id_from_query():
    f = media_urls.session_id_from_query
    assert f("s=12") == 12 and f("a=1&s=12") == 12
    assert f(None) is None and f("") is None and f("s=") is None and f("s=x") is None
    assert f("s=-1") is None and f("s=1&s=2") is None and f("s=" + "9" * 30) is None
