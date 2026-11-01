import fakeredis
from django.test import Client

from monkeurl import services
from monkeurl.models import DailyClick, Referrer, URLR

from .base import RedisTestCase


class Persistence(RedisTestCase):
    """Drop every in-memory structure and prove the DB held its ground."""

    def test_data_survives_a_simulated_restart(self):
        res = self.client.post(
            "/api/links/", data='{"origin": "https://example.com/durable"}',
            content_type="application/json")
        code = res.json()["id"]
        self.client.get(f"/{code}/", HTTP_REFERER="https://news.site/x")
        self.client.get(f"/{code}/")
        services.flush_all_pending()

        # simulate restart: new client object, new redis, empty LRU
        services.reset_redis(fakeredis.FakeStrictRedis(decode_responses=True))
        services._lru.clear()
        fresh = Client()

        stats = fresh.get(f"/api/links/{code}/")
        self.assertEqual(stats.json()["clicks"], 2)

        redir = fresh.get(f"/{code}/")
        self.assertEqual(redir.status_code, 301)
        self.assertEqual(redir["Location"], "https://example.com/durable")
        self.assertEqual(services.pending_clicks(code), 1)

        link = URLR.all_objects.get(pk=code)
        self.assertFalse(link.deleted)
        self.assertEqual(DailyClick.objects.get(link_id=code).count, 2)
        refs = {r.name: r.count
                for r in Referrer.objects.filter(link_id=code)}
        self.assertEqual(refs, {"https://news.site/x": 1, "direct": 1})

    def test_bad_requests_do_not_corrupt_state(self):
        for i in range(10):
            self.client.post("/api/links/", data="{invalid json",
                             content_type="application/json")
            self.client.get("/definitely-missing/")
        self.assertTrue(URLR.all_objects.count() == 0)
