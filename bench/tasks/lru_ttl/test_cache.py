import threading
import unittest

from cache import TTLCache


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


class TTLCacheTest(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.cache = TTLCache(capacity=3, ttl=10, clock=self.clock)

    def test_get_set(self):
        self.cache.set("a", 1)
        self.assertEqual(self.cache.get("a"), 1)
        self.assertEqual(self.cache.get("b", "x"), "x")

    def test_rejects_bad_arguments(self):
        with self.assertRaises(ValueError):
            TTLCache(capacity=0, ttl=1)
        with self.assertRaises(ValueError):
            TTLCache(capacity=1, ttl=0)

    def test_expiry_not_extended_by_get(self):
        self.cache.set("a", 1)
        self.clock.now = 9
        self.assertEqual(self.cache.get("a"), 1)
        self.clock.now = 10
        self.assertIsNone(self.cache.get("a"))

    def test_per_call_ttl(self):
        self.cache.set("a", 1, ttl=2)
        self.clock.now = 2
        self.assertNotIn("a", self.cache)

    def test_lru_eviction(self):
        for k in "abc":
            self.cache.set(k, k)
        self.cache.get("a")
        self.cache.set("d", "d")
        self.assertNotIn("b", self.cache)
        self.assertIn("a", self.cache)
        self.assertEqual(self.cache.stats()["evictions"], 1)

    def test_expired_removed_before_live_eviction(self):
        self.cache.set("a", 1, ttl=1)
        self.cache.set("b", 2)
        self.cache.set("c", 3)
        self.clock.now = 5
        self.cache.set("d", 4)
        self.assertEqual(len(self.cache), 3)
        self.assertIn("b", self.cache)
        self.assertEqual(self.cache.stats()["evictions"], 0)
        self.assertEqual(self.cache.stats()["expirations"], 1)

    def test_contains_does_not_touch_recency(self):
        for k in "abc":
            self.cache.set(k, k)
        self.assertIn("a", self.cache)
        self.cache.set("d", "d")
        self.assertNotIn("a", self.cache)

    def test_overwrite_refreshes(self):
        self.cache.set("a", 1)
        self.clock.now = 8
        self.cache.set("a", 2)
        self.clock.now = 15
        self.assertEqual(self.cache.get("a"), 2)
        self.assertEqual(len(self.cache), 1)

    def test_delete_and_stats(self):
        self.cache.set("a", 1)
        self.assertTrue(self.cache.delete("a"))
        self.assertFalse(self.cache.delete("a"))
        self.cache.get("a")
        self.cache.set("b", 2)
        self.cache.get("b")
        self.assertEqual(self.cache.stats(), {"hits": 1, "misses": 1, "evictions": 0, "expirations": 0})

    def test_thread_safety(self):
        cache = TTLCache(capacity=50, ttl=100)

        def work(n):
            for i in range(2000):
                cache.set((n, i % 60), i)
                cache.get((n, (i * 7) % 60))

        threads = [threading.Thread(target=work, args=(n,)) for n in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertLessEqual(len(cache), 50)
