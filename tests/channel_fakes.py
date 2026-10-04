"""A scripted ChannelLister for subscription tests (CS-1..CS-7): no network, no yt-dlp."""

from __future__ import annotations

from datetime import UTC, datetime

from tellybox.ytdlp import ChannelEntry, ChannelListing, ChannelTab, VideoStatus, YtDlpError, watch_url

PUBLISHED = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)


def entry(youtube_id: str, *, title: str | None = None, duration_s: float | None = 300.0, short: bool = False,
          live_status: str | None = None) -> ChannelEntry:
    return ChannelEntry(
        youtube_id=youtube_id, url=watch_url(youtube_id), title=title or f"Video {youtube_id}",
        duration_s=None if short else duration_s, thumbnail_url=f"https://i.ytimg.com/vi/{youtube_id}/hq.jpg",
        approx_date=PUBLISHED, is_short=short, live_status=live_status,
    )


class FakeChannelLister:
    """Channels with newest-first listings per tab and per-video statuses.

    A URL resolves to a channel when it contains the channel id or its handle. `fail` makes every call
    raise (a channel that disappeared, or YouTube being down).
    """

    def __init__(self) -> None:
        self.channels: dict[str, dict] = {}
        self.statuses: dict[str, VideoStatus] = {}
        self.fail: Exception | None = None
        self.calls: list[tuple[str, str, int, int]] = []  # (channel_id, tab, offset, limit)
        self.status_calls: list[str] = []

    def add_channel(self, channel_id: str, name: str, handle: str | None = None,
                    videos: list[str] | None = None, shorts: list[str] | None = None) -> None:
        self.channels[channel_id] = {"name": name, "handle": handle or channel_id,
                                     "videos": [entry(v) for v in videos or []],
                                     "shorts": [entry(s, short=True) for s in shorts or []]}

    def upload(self, channel_id: str, youtube_id: str, *, short: bool = False, **kw) -> None:
        """A new upload goes to the front of its tab."""
        tab = "shorts" if short else "videos"
        self.channels[channel_id][tab].insert(0, entry(youtube_id, short=short, **kw))

    def set_status(self, youtube_id: str, **kw) -> None:
        current = self.statuses.get(youtube_id, VideoStatus(PUBLISHED, "not_live", "public", None))
        self.statuses[youtube_id] = VideoStatus(**{**current.__dict__, **kw})

    def _channel(self, url: str) -> tuple[str, dict]:
        for channel_id, ch in self.channels.items():
            if channel_id in url or ch["handle"] in url:
                return channel_id, ch
        raise YtDlpError("Channel not found: HTTP Error 404: Not Found", retryable=False)

    def list_channel(self, url: str, tab: ChannelTab, offset: int, limit: int) -> ChannelListing:
        if self.fail:
            raise self.fail
        channel_id, ch = self._channel(url)
        self.calls.append((channel_id, tab, offset, limit))
        return ChannelListing(channel_id, ch["name"], ch[tab][offset:offset + limit])

    def video_status(self, youtube_id: str) -> VideoStatus:
        if self.fail:
            raise self.fail
        self.status_calls.append(youtube_id)
        return self.statuses.get(youtube_id, VideoStatus(PUBLISHED, "not_live", "public", None))
