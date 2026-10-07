"""Typed events (HA-13): a fan-out of discrete events for the admin API's `?typed=1` stream.

Events are advisory edges: not stored, not replayed. The hub merges what the cast service emits (relayed from
its `GET /typed-events`) with what the web service derives from the database, and gives each connection a
bounded queue that drops the oldest event when full, so a slow client never blocks the others.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

from tellybox.web.cast_client import CastUnavailable

log = logging.getLogger(__name__)

QUEUE_SIZE = 100


class EventHub:
    def __init__(
        self,
        cast=None,
        *,
        queue_size: int = QUEUE_SIZE,
        min_backoff: float = 1.0,
        max_backoff: float = 5.0,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.cast = cast
        self.queue_size = queue_size
        self.min_backoff = min_backoff
        self.max_backoff = max_backoff
        self._sleep = sleep
        self._subscribers: set[asyncio.Queue] = set()
        self._overflowing: set[asyncio.Queue] = set()
        self._task: asyncio.Task | None = None

    # ------------------------------------------------------------------ fan-out

    def subscribe(self) -> asyncio.Queue:
        """An empty queue: a subscriber sees only events published while it is subscribed."""
        q: asyncio.Queue = asyncio.Queue(maxsize=self.queue_size)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subscribers.discard(q)
        self._overflowing.discard(q)

    def publish(self, event: dict) -> None:
        for q in list(self._subscribers):
            if q.full():
                q.get_nowait()  # drop the oldest
                if q not in self._overflowing:  # log once per overflow episode, not per event
                    self._overflowing.add(q)
                    log.warning("typed event subscriber is slow: dropping its oldest events")
            else:
                self._overflowing.discard(q)
            q.put_nowait(event)

    # ------------------------------------------------------------------ cast relay

    def start(self) -> None:
        if self._task is None and self.cast is not None:
            self._task = asyncio.create_task(self.run(), name="typed-event-relay")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def run(self) -> None:
        """Relay the cast service's typed events; while it is down typed frames just pause."""
        delay = self.min_backoff
        down = False  # log once per outage, not on every retry
        while True:
            try:
                async for event in self.cast.typed_events():
                    self.publish(event)
                    delay, down = self.min_backoff, False
                reason = "typed event stream ended"
            except CastUnavailable as exc:
                reason = str(exc)
            except Exception as exc:  # keep relaying whatever goes wrong
                log.exception("typed event relay failed")
                reason = repr(exc)
            if not down:
                log.warning("cast service typed events unreachable, retrying: %s", reason)
                down = True
            await self._sleep(delay)
            delay = min(delay * 2, self.max_backoff)
