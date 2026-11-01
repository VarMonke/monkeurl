import json

from monkeurl import services
from monkeurl.models import URLR

from .base import RedisTestCase


class EndToEndFlow(RedisTestCase):
    """The whole happy path, soup to nuts."""

    def test_001_a_user_shortens_a_url(self):
        res = self.client.post(
            "/api/links/",
            data=json.dumps({"origin": "https://example.com/a/b/c"}),
            content_type="application/json",
        )
        self.assertEqual(res.status_code, 201)
        self.link_id = res.json()["id"]
        self.assertEqual(len(self.link_id), 7)
        self.assertEqual(res.json()["origin"], "https://example.com/a/b/c")

    def test_002_the_link_redirects(self):
        link = URLR.objects.create(
            id="e2elink", origin="https://example.com/dest")
        res = self.client.get(f"/{link.id}/")
        self.assertEqual(res.status_code, 301)
        self.assertEqual(res["Location"], "https://example.com/dest")

    def test_003_clicks_are_counted(self):
        link = URLR.objects.create(
            id="e2eclick", origin="https://example.com/dest")
        for _ in range(3):
            self.client.get(f"/{link.id}/")
        # buffered in redis, merged into the stats view immediately
        res = self.client.get(f"/api/links/{link.id}/")
        self.assertEqual(res.json()["clicks"], 3)
        services.flush_all_pending()
        link.refresh_from_db()
        self.assertEqual(link.clicks, 3)

    def test_004_nobody_dies(self):
        res = self.client.get("/healthz")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["status"], "alive")

    def test_new_users_get_a_profile_and_own_their_links(self):
        from django.contrib.auth import get_user_model
        user = get_user_model().objects.create_user(
            username="e2e-owner", password="x")
        profile = user.profile  # signal should have made this
        self.client.force_login(user)
        res = self.client.post(
            "/api/links/",
            data=json.dumps({"origin": "https://example.com/owned"}),
            content_type="application/json")
        link = URLR.all_objects.get(pk=res.json()["id"])
        self.assertEqual(link.created_by, profile)

    def test_full_round_trip(self):
        create = self.client.post(
            "/api/links/",
            data=json.dumps({"origin": "https://example.com/full",
                             "alias": "round-trip"}),
            content_type="application/json",
        )
        self.assertEqual(create.status_code, 201)
        redir = self.client.get("/round-trip/")
        self.assertEqual(redir.status_code, 301)
        self.assertEqual(redir["Location"], "https://example.com/full")
        stats = self.client.get("/api/links/round-trip/")
        self.assertEqual(stats.json()["clicks"], 1)
        page = self.client.get("/stats/round-trip/")
        self.assertEqual(page.status_code, 200)
