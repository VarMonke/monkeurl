import json
from concurrent.futures import ThreadPoolExecutor

import fakeredis
from django.test import Client, TransactionTestCase, override_settings

from monkeurl import services
from monkeurl.models import URLR


@override_settings(
    SHORTENER_RATE_LIMIT=1_000_000,
    SHORTENER_CLICK_FLUSH_THRESHOLD=1_000_000,  # defer all flushes to the end
)
class Concurrency(TransactionTestCase):
    """Competing writers, shared in-memory Redis, no mercy."""

    def setUp(self):
        super().setUp()
        self.redis = fakeredis.FakeStrictRedis(decode_responses=True)
        services.reset_redis(self.redis)
        services._lru.clear()

    def tearDown(self):
        services.reset_redis()
        services._lru.clear()
        super().tearDown()

    def test_two_dozen_users_fight_over_one_alias_and_nobody_dies(self):
        workers = 24
        payload = json.dumps(
            {"origin": "https://example.com", "alias": "contested"})

        def attempt(i):
            return Client().post("/api/links/", data=payload,
                                 content_type="application/json").status_code

        with ThreadPoolExecutor(max_workers=workers) as pool:
            codes = list(pool.map(attempt, range(workers)))

        self.assertEqual(codes.count(201), 1)
        self.assertEqual(codes.count(409), workers - 1)
        self.assertEqual(URLR.all_objects.filter(pk="contested").count(), 1)

    def test_click_storm_counts_every_click_exactly_once(self):
        URLR.objects.create(id="storm", origin="https://example.com/storm")
        workers, clicks_each = 10, 5

        def click(i):
            return Client().get("/storm/").status_code

        with ThreadPoolExecutor(max_workers=workers) as pool:
            codes = list(pool.map(click, range(workers * clicks_each)))

        self.assertEqual(set(codes), {301})
        services.flush_all_pending()
        link = URLR.all_objects.get(pk="storm")
        self.assertEqual(link.clicks, workers * clicks_each)
        self.assertEqual(services.pending_clicks("storm"), 0)

    def test_concurrent_distinct_creates_get_unique_codes(self):
        workers = 20

        def attempt(i):
            res = Client().post(
                "/api/links/",
                data=json.dumps({"origin": f"https://example.com/{i}"}),
                content_type="application/json")
            return res.status_code, res.json().get("id")

        with ThreadPoolExecutor(max_workers=workers) as pool:
            results = list(pool.map(attempt, range(workers)))

        statuses = {s for s, _ in results}
        ids = {i for _, i in results}
        self.assertEqual(statuses, {201})
        self.assertEqual(len(ids), workers)  # all unique
        self.assertEqual(URLR.all_objects.count(), workers)
