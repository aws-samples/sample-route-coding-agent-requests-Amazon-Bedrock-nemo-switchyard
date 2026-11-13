from collections.abc import Callable
import time


class TTLCache:
    """A thread-safe LRU cache where each entry also expires after a time to live.

    - capacity: the most entries kept. Adding past capacity evicts the least recently used live entry.
      Expired entries are removed before anything live is evicted.
    - ttl: seconds an entry lives after it is set. get() does not extend the ttl.
    - clock: returns the current time in seconds. Defaults to time.monotonic.
    - get(key, default=None) returns the value or default, and marks the key as recently used.
    - set(key, value, ttl=None) stores a value. A per-call ttl overrides the default.
    - delete(key) returns True if a live entry was removed.
    - len(cache) counts only live entries. `key in cache` is True only for live entries
      and does not change recency.
    - stats() returns a dict with hits, misses, evictions (capacity only) and expirations.
    """

    def __init__(self, capacity: int, ttl: float, clock: Callable[[], float] = time.monotonic) -> None:
        raise NotImplementedError
