import fakeredis
from django.test import TestCase, override_settings

from monkeurl import services


@override_settings(
    SHORTENER_RATE_LIMIT=10_000,          # don't trip the limiter by accident
    SHORTENER_STRICT_DESTINATION=True,
    SHORTENER_CLICK_FLUSH_THRESHOLD=100,
)
class RedisTestCase(TestCase):
    """Base class: swaps in a fresh fakeredis + empty LRU per test."""

    def setUp(self):
        super().setUp()
        self.redis = fakeredis.FakeStrictRedis(decode_responses=True)
        services.reset_redis(self.redis)
        services._lru.clear()

    def tearDown(self):
        services.reset_redis()
        services._lru.clear()
        super().tearDown()
