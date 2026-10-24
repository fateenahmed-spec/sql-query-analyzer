"""Small process-local protections used when the service runs publicly."""

from __future__ import annotations

import threading
import time
from collections import defaultdict, deque
from collections.abc import Callable


class InMemoryRateLimiter:
    """Thread-safe rolling-window limiter; returns retry seconds or ``None``."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._events: dict[tuple[str, str], deque[float]] = defaultdict(deque)
        self._windows: dict[tuple[str, str], float] = {}
        self._lock = threading.Lock()
        self._operations = 0

    def consume(
        self,
        client: str,
        bucket: str,
        limit: int,
        window_seconds: float,
    ) -> float | None:
        now = self._clock()
        cutoff = now - window_seconds
        key = (client, bucket)
        with self._lock:
            self._operations += 1
            if self._operations % 256 == 0:
                for stale_key, stale_events in list(self._events.items()):
                    stale_window = self._windows[stale_key]
                    while stale_events and stale_events[0] <= now - stale_window:
                        stale_events.popleft()
                    if not stale_events:
                        self._events.pop(stale_key, None)
                        self._windows.pop(stale_key, None)

            self._windows[key] = window_seconds
            events = self._events[key]
            while events and events[0] <= cutoff:
                events.popleft()
            if len(events) >= limit:
                return max(0.001, window_seconds - (now - events[0]))
            events.append(now)
            return None
