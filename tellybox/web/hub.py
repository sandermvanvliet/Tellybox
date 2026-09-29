"""Live kid state (KA-7): one relay of the cast service's event stream, fanned out
to every open kid page over SSE.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable

from tellybox.web.cast_client import CastUnavailable

log = logging.getLogger(__name__)

SSE_KEEPALIVE_S = 15.0

# Before the first cast state arrives nothing is known: TV unreachable, sky unknown.
INITIAL_STATE: dict = {
    "tv": "unreachable",
    "now_playing": None,
    "sky": {"fraction_left": None, "last_five": False, "unlimited": False},
    "time_up": False,
    "watching": [],
    "profiles": {},
    "day": None,
}


def unreachable(state: dict) -> dict:
    """The TV can't be reached: keep the last known sky and time_up, drop now playing."""
    return {**state, "tv": "unreachable", "now_playing": None}


_kid_unreachable = unreachable  # KidHub's `unreachable` parameter shadows the name


class KidHub:
    """Relays the cast service's stream through `reduce`. The admin API (HA-3) reuses it with its own
    `reduce`, `unreachable` and `initial`, and `refresh_s` so the parts that don't come from the cast
    service (jobs, disk, profile names) also reach the stream.
    """

    def __init__(
        self,
        cast,
        reduce: Callable[[dict], dict],
        *,
        unreachable: Callable[[dict], dict] | None = None,
        initial: Callable[[], dict] | None = None,
        refresh_s: float | None = None,
        min_backoff: float = 1.0,
        max_backoff: float = 5.0,
        queue_size: int = 20,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.cast = cast
        self.reduce = reduce
        self._unreachable = unreachable or _kid_unreachable
        self._initial = initial
        self.refresh_s = refresh_s
        self.min_backoff = min_backoff
        self.max_backoff = max_backoff
        self.queue_size = queue_size
        self._sleep = sleep
        self.state: dict = initial() if initial else INITIAL_STATE
        self._subscribers: set[asyncio.Queue] = set()
        self._task: asyncio.Task | None = None
        self._refresh_task: asyncio.Task | None = None
        self._last_cast: dict | None = None  # the newest cast state, for refreshes
        self._down = False

    # ------------------------------------------------------------------ fan-out

    def subscribe(self) -> asyncio.Queue:
        """A queue that starts with the current state, then gets every change."""
        q: asyncio.Queue = asyncio.Queue(maxsize=self.queue_size)
        q.put_nowait(self.state)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subscribers.discard(q)

    def publish(self, state: dict) -> None:
        if state == self.state:
            return
        self.state = state
        for q in self._subscribers:
            if q.full():  # a slow page only needs the latest state
                q.get_nowait()
            q.put_nowait(state)

    # ------------------------------------------------------------------ relay loop

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self.run(), name="kid-hub")
        if self.refresh_s is not None and self._refresh_task is None:
            self._refresh_task = asyncio.create_task(self._refresh_loop(), name="hub-refresh")

    async def stop(self) -> None:
        tasks = [t for t in (self._task, self._refresh_task) if t is not None]
        self._task = self._refresh_task = None
        for task in tasks:
            task.cancel()
        for task in tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass

    def _current(self) -> dict:
        """The reduction of the newest cast state (or the initial state before any), as the outage left it."""
        if self._last_cast is not None:
            state = self.reduce(self._last_cast)
        elif self._initial is not None:
            state = self._initial()
        else:
            return self.state
        return self._unreachable(state) if self._down else state

    async def _refresh_loop(self) -> None:
        while True:
            await asyncio.sleep(self.refresh_s)
            try:
                self.publish(self._current())
            except Exception:
                log.exception("hub refresh failed")

    async def run(self) -> None:
        delay = self.min_backoff
        down = False  # log once per outage, not on every retry
        while True:
            try:
                async for cast_state in self.cast.events():
                    self._last_cast, self._down = cast_state, False
                    self.publish(self.reduce(cast_state))
                    delay, down = self.min_backoff, False
                reason = "event stream ended"
            except CastUnavailable as exc:
                reason = str(exc)
            except Exception as exc:  # keep relaying whatever goes wrong
                log.exception("kid hub relay failed")
                reason = repr(exc)
            if not down:
                log.warning("cast service unreachable, retrying: %s", reason)
                down = True
            self._down = True
            self.publish(self._unreachable(self.state))
            await self._sleep(delay)
            delay = min(delay * 2, self.max_backoff)


async def sse_stream(hub: KidHub, keepalive_s: float = SSE_KEEPALIVE_S) -> AsyncIterator[str]:
    """SSE body: the current state immediately, then one event per change (KA-7)."""
    q = hub.subscribe()
    try:
        while True:
            try:
                state = await asyncio.wait_for(q.get(), keepalive_s)
            except TimeoutError:
                yield ": keepalive\n\n"
                continue
            yield f"data: {json.dumps(state)}\n\n"
    finally:
        hub.unsubscribe(q)
