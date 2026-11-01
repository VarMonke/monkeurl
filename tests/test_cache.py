import fakeredis
from django.test import override_settings

from monkeurl import services
from monkeurl.models import DailyClick, Referrer, URLR

from .base import RedisTestCase


class CacheLayers(RedisTestCase):
    def setUp(self):
        super().setUp()
        self.link = URLR.objects.create(
            id="hotlink", origin="https://example.com/hot")

    def test_second_resolve_hits_lru_zero_db_queries(self):
        services.resolve("hotlink")  # warms LRU + redis
        services.get_redis().delete("url:hotlink")  # prove LRU alone serves it
        with self.assertNumQueries(0):
            res = services.resolve("hotlink")
        self.assertEqual(res.status, "ok")

    def test_redis_layer_serves_after_lru_evict(self):
        services.resolve("hotlink")  # warms both
        services._lru.clear()
        with self.assertNumQueries(0):
            res = services.resolve("hotlink")
        self.assertEqual(res.status, "ok")
        self.assertEqual(res.origin, "https://example.com/hot")

    def test_missing_code_is_negatively_cached(self):
        services.resolve("nope404")
        with self.assertNumQueries(0):
            res = services.resolve("nope404")
        self.assertEqual(res.status, "missing")

    def test_delete_evicts_and_redirects_410(self):
        services.resolve("hotlink")
        self.link.soft_delete()
        services.evict_code("hotlink")
        with self.assertNumQueries(1):
            res = services.resolve("hotlink")
        self.assertEqual(res.status, "deleted")

    def test_delete_without_evict_still_gone_after_refresh(self):
        # LRU miss -> redis miss/deleted flag -> DB truth
        self.link.soft_delete()
        services.reset_redis(fakeredis.FakeStrictRedis(decode_responses=True))
        services._lru.clear()
        res = services.resolve("hotlink")
        self.assertEqual(res.status, "deleted")


class ClickBuffering(RedisTestCase):
    def setUp(self):
        super().setUp()
        self.link = URLR.objects.create(
            id="buffered", origin="https://example.com/b")

    def test_clicks_buffer_in_redis_not_db(self):
        for _ in range(3):
            self.client.get("/buffered/")
        self.link.refresh_from_db()
        self.assertEqual(self.link.clicks, 0)
        self.assertEqual(services.pending_clicks("buffered"), 3)

    def test_flush_writes_db_counts(self):
        for _ in range(7):
            self.client.get("/buffered/")
        services.flush_all_pending()
        self.link.refresh_from_db()
        self.assertEqual(self.link.clicks, 7)
        self.assertEqual(services.pending_clicks("buffered"), 0)

    def test_daily_clicks_tracked(self):
        for _ in range(4):
            self.client.get("/buffered/")
        services.flush_all_pending()
        today = DailyClick.objects.get(link_id="buffered")
        self.assertEqual(today.count, 4)

    def test_referrers_tracked(self):
        self.client.get("/buffered/", HTTP_REFERER="https://news.site/a")
        self.client.get("/buffered/", HTTP_REFERER="https://news.site/a")
        self.client.get("/buffered/")  # direct
        services.flush_all_pending()
        refs = {r.name: r.count
                for r in Referrer.objects.filter(link_id="buffered")}
        self.assertEqual(refs["https://news.site/a"], 2)
        self.assertEqual(refs["direct"], 1)

    @override_settings(SHORTENER_CLICK_FLUSH_THRESHOLD=5)
    def test_threshold_triggers_inline_flush(self):
        for _ in range(5):
            self.client.get("/buffered/")
        self.link.refresh_from_db()
        self.assertEqual(self.link.clicks, 5)

    def test_referrer_is_truncated_not_fatal(self):
        res = self.client.get("/buffered/", HTTP_REFERER="https://" + "x" * 400)
        self.assertEqual(res.status_code, 301)

    def test_redis_outage_degrades_to_direct_write(self):
        import redis as redis_lib
        # port 1 is deterministically dead, whatever tunnels are running
        dead = redis_lib.Redis(
            host="127.0.0.1", port=1,
            socket_connect_timeout=0.1, socket_timeout=0.1,
            decode_responses=True)
        services.reset_redis(dead)
        res = self.client.get("/buffered/")
        self.assertEqual(res.status_code, 301)
        self.link.refresh_from_db()
        self.assertEqual(self.link.clicks, 1)
