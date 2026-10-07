"""Typed events of the cast service (HA-13): the dict builders and the bus behind ``GET /typed-events``.

Events are advisory edges, not stored and not replayed (see docs/cast-api.md, "Typed events"). Every event is
``{"type", "at", ...fields}`` with ``at`` an ISO-8601 UTC string. The controller queues them while it works and
hands them to the bus right after the state broadcast for the same cause, so a consumer that handles an event
has seen a state at least as new.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Collection
from datetime import datetime

from tellybox.db import to_db

log = logging.getLogger(__name__)

QUEUE_SIZE = 100  # per subscriber; the oldest event is dropped when a slow reader's queue is full


def _ids(profile_ids: Collection[int]) -> list[int]:
    return sorted(profile_ids)


def _envelope(event_type: str, at: datetime, **fields: object) -> dict:
    return {"type": event_type, "at": to_db(at), **fields}


def playback_started(
    at: datetime, *, profile_ids: Collection[int], episode_id: int, show_id: int, title: str, show: str | None,
    target: str, label: str | None,
) -> dict:
    return _envelope(
        "playback_started", at, profile_ids=_ids(profile_ids), episode_id=episode_id, show_id=show_id,
        title=title, show=show, target=target, label=label,
    )


def playback_stopped(
    at: datetime, *, profile_ids: Collection[int], episode_id: int, show_id: int, title: str, show: str | None,
    target: str, label: str | None, reason: str, position_s: float,
) -> dict:
    return _envelope(
        "playback_stopped", at, profile_ids=_ids(profile_ids), episode_id=episode_id, show_id=show_id,
        title=title, show=show, target=target, label=label, reason=str(reason), position_s=round(position_s),
    )


def time_up(at: datetime, *, profile_ids: Collection[int], reason: str) -> dict:
    return _envelope("time_up", at, profile_ids=_ids(profile_ids), reason=str(reason))


def last_five(at: datetime, *, profile_ids: Collection[int], remaining_s: float) -> dict:
    return _envelope("last_five", at, profile_ids=_ids(profile_ids), remaining_s=round(remaining_s))


def override_applied(
    at: datetime, *, kind: str, value: int | None, profile_ids: Collection[int], source: str | None
) -> dict:
    return _envelope("override_applied", at, kind=kind, value=value, profile_ids=_ids(profile_ids), source=source)


class EventBus:
    """Fan-out of events to subscribers; ``publish`` never blocks."""

    def __init__(self, maxsize: int = QUEUE_SIZE) -> None:
        self._maxsize = maxsize
        self._subscribers: dict[asyncio.Queue, bool] = {}  # queue -> an overflow was logged and not yet recovered

    def __bool__(self) -> bool:
        return bool(self._subscribers)

    def subscribe(self) -> asyncio.Queue:
        """A new queue that sees events published from now on (no replay)."""
        q: asyncio.Queue = asyncio.Queue(maxsize=self._maxsize)
        self._subscribers[q] = False
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subscribers.pop(q, None)

    def publish(self, event: dict) -> None:
        for q, overflowing in list(self._subscribers.items()):
            if q.full():
                q.get_nowait()  # drop the oldest
                if not overflowing:  # log once per overflow episode, not per event
                    log.warning("a typed-events subscriber is too slow; dropping its oldest events")
                    self._subscribers[q] = True
            elif overflowing:
                self._subscribers[q] = False  # the reader caught up: the next overflow is a new episode
            q.put_nowait(event)
