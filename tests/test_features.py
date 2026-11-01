import json
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import override_settings
from django.utils import timezone

from monkeurl import services
from monkeurl.models import URLR

from .base import RedisTestCase

User = get_user_model()


def create(url, **kw):
    return json.dumps({"origin": url, **kw})


class CustomAliases(RedisTestCase):
    def test_alias_is_used_instead_of_random(self):
        res = self.client.post("/api/links/",
                               data=create("https://example.com",
                                           alias="my-alias"),
                               content_type="application/json")
        self.assertEqual(res.status_code, 201)
        self.assertEqual(res.json()["id"], "my-alias")

    def test_alias_conflict_returns_409(self):
        self.client.post("/api/links/",
                         data=create("https://example.com", alias="taken"),
                         content_type="application/json")
        res = self.client.post("/api/links/",
                               data=create("https://other.com", alias="taken"),
                               content_type="application/json")
        self.assertEqual(res.status_code, 409)

    def test_deleted_alias_stays_taken(self):
        link = URLR.objects.create(id="ghost", origin="https://example.com")
        link.soft_delete()
        res = self.client.post("/api/links/",
                               data=create("https://example.com",
                                           alias="ghost"),
                               content_type="application/json")
        self.assertEqual(res.status_code, 409)

    def test_reserved_word_rejected(self):
        res = self.client.post("/api/links/",
                               data=create("https://example.com",
                                           alias="admin"),
                               content_type="application/json")
        self.assertEqual(res.status_code, 400)

    def test_milhouse_is_reserved_for_reasons_we_do_not_discuss(self):
        res = self.client.post("/api/links/",
                               data=create("https://example.com",
                                           alias="milhouse"),
                               content_type="application/json")
        self.assertEqual(res.status_code, 400)

    def test_alias_with_bad_chars_rejected(self):
        res = self.client.post("/api/links/",
                               data=create("https://example.com",
                                           alias="has space"),
                               content_type="application/json")
        self.assertEqual(res.status_code, 400)


class Expiry(RedisTestCase):
    def test_ttl_sets_expiry(self):
        res = self.client.post(
            "/api/links/",
            data=create("https://example.com", ttl_seconds=3600),
            content_type="application/json")
        self.assertEqual(res.status_code, 201)
        link = URLR.objects.get(pk=res.json()["id"])
        self.assertIsNotNone(link.expires_at)

    def test_expired_link_returns_410(self):
        link = URLR.objects.create(id="oldlink", origin="https://example.com",
                                   expires_at=timezone.now()
                                   - timedelta(seconds=1))
        services.evict_code(link.id)
        res = self.client.get(f"/{link.id}/")
        self.assertEqual(res.status_code, 410)

    def test_live_link_does_not_expire(self):
        link = URLR.objects.create(
            id="fresh", origin="https://example.com",
            expires_at=timezone.now() + timedelta(hours=1))
        res = self.client.get(f"/{link.id}/")
        self.assertEqual(res.status_code, 301)


class RateLimiting(RedisTestCase):
    @override_settings(SHORTENER_RATE_LIMIT=3, SHORTENER_RATE_WINDOW=60)
    def test_rate_limiter_tells_karen_to_calm_down(self):
        codes = []
        for i in range(5):
            res = self.client.post(
                "/api/links/",
                data=create(f"https://example.com/{i}"),
                content_type="application/json")
            codes.append(res.status_code)
        self.assertEqual(codes[:3], [201, 201, 201])
        self.assertEqual(codes[3:], [429, 429])

    @override_settings(SHORTENER_RATE_LIMIT=2, SHORTENER_RATE_WINDOW=60)
    def test_429_response_states_the_actual_limit(self):
        self.client.post("/api/links/", data=create("https://a.com"),
                         content_type="application/json")
        self.client.post("/api/links/", data=create("https://b.com"),
                         content_type="application/json")
        res = self.client.post("/api/links/", data=create("https://c.com"),
                               content_type="application/json")
        self.assertEqual(res.status_code, 429)
        self.assertIn("2", res.json()["detail"])


class AbuseProtection(RedisTestCase):
    def test_javascript_scheme_rejected(self):
        res = self.client.post("/api/links/",
                               data=create("javascript:alert(1)"),
                               content_type="application/json")
        self.assertEqual(res.status_code, 400)

    def test_ftp_scheme_rejected(self):
        res = self.client.post("/api/links/",
                               data=create("ftp://example.com/file"),
                               content_type="application/json")
        self.assertEqual(res.status_code, 400)

    def test_loopback_destination_rejected(self):
        res = self.client.post("/api/links/",
                               data=create("http://127.0.0.1:9999/x"),
                               content_type="application/json")
        self.assertEqual(res.status_code, 400)
        self.assertIn("internal", res.json()["error"].lower())

    def test_aws_metadata_endpoint_rejected(self):
        res = self.client.post(
            "/api/links/",
            data=create("http://169.254.169.254/latest/meta-data"),
            content_type="application/json")
        self.assertEqual(res.status_code, 400)

    @override_settings(SHORTENER_OWN_HOSTS=["example.com"])
    def test_self_redirect_rejected(self):
        res = self.client.post(
            "/api/links/",
            data=create("https://example.com/some-short-code"),
            content_type="application/json")
        self.assertEqual(res.status_code, 400)
        self.assertIn("point back", res.json()["error"])

    def test_unresolvable_host_rejected(self):
        res = self.client.post(
            "/api/links/",
            data=create("http://nonexistent.invalid-domain-xyz123/"),
            content_type="application/json")
        self.assertEqual(res.status_code, 400)

    def test_empty_and_garbage_urls_rejected(self):
        for bad in ["", "notaurl", "http://", "://missing-scheme.com"]:
            res = self.client.post("/api/links/", data=create(bad),
                                   content_type="application/json")
            self.assertEqual(res.status_code, 400, msg=bad)


class Deletion(RedisTestCase):
    def _make_user(self, name):
        user = User.objects.create_user(username=name, password="x")
        return user, user.profile  # signal auto-creates GugolUser

    def test_owner_can_delete(self):
        user, profile = self._make_user("alice")
        link = URLR.objects.create(id="mine", origin="https://example.com",
                                   created_by=profile)
        self.client.force_login(user)
        res = self.client.delete("/api/links/mine/")
        self.assertEqual(res.status_code, 204)
        link.refresh_from_db()
        self.assertTrue(link.deleted)

    def test_other_user_cannot_delete(self):
        _, profile_a = self._make_user("alice")
        user_b, _ = self._make_user("mallory")
        URLR.objects.create(id="alices", origin="https://example.com",
                            created_by=profile_a)
        self.client.force_login(user_b)
        res = self.client.delete("/api/links/alices/")
        self.assertEqual(res.status_code, 403)

    def test_anonymous_link_cannot_be_deleted(self):
        URLR.objects.create(id="feral", origin="https://example.com")
        res = self.client.delete("/api/links/feral/")
        self.assertEqual(res.status_code, 403)

    def test_deleted_link_redirects_410(self):
        link = URLR.objects.create(id="rip", origin="https://example.com")
        link.soft_delete()
        services.evict_code("rip")
        res = self.client.get("/rip/")
        self.assertEqual(res.status_code, 410)

    def test_there_is_no_edit_endpoint(self):
        link = URLR.objects.create(id="immutable",
                                   origin="https://example.com")
        res = self.client.patch(
            f"/api/links/{link.id}/",
            data=json.dumps({"origin": "https://evil.com"}),
            content_type="application/json")
        self.assertEqual(res.status_code, 405)
        link.refresh_from_db()
        self.assertEqual(link.origin, "https://example.com")
