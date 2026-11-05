import unittest

from cache import Cache


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class TestCache(unittest.TestCase):
    def test_no_ttl(self):
        cache = Cache()
        cache.set("a", 1)
        self.assertEqual(cache.get("a"), 1)
        self.assertEqual(cache.get("missing", "x"), "x")
        self.assertEqual(len(cache), 1)

    def test_ttl_expires(self):
        clock = FakeClock()
        cache = Cache(clock=clock)
        cache.set("a", 1, ttl=10)
        cache.set("b", 2)
        clock.now += 5
        self.assertEqual(cache.get("a"), 1)
        clock.now += 6
        self.assertIsNone(cache.get("a"))
        self.assertEqual(cache.get("a", "gone"), "gone")
        self.assertEqual(cache.get("b"), 2)

    def test_len_ignores_expired(self):
        clock = FakeClock()
        cache = Cache(clock=clock)
        cache.set("a", 1, ttl=1)
        cache.set("b", 2, ttl=100)
        cache.set("c", 3)
        self.assertEqual(len(cache), 3)
        clock.now += 2
        self.assertEqual(len(cache), 2)


if __name__ == "__main__":
    unittest.main()
