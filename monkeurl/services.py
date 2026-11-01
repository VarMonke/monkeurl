import ipaddress
import json
import logging
import secrets
import socket
import threading
from dataclasses import dataclass
from datetime import date
from urllib.parse import urlparse

import redis as redis_lib
from cachetools import TTLCache
from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from django.db.models import F

from .models import DailyClick, Referrer, URLR

log = logging.getLogger(__name__)

BASE62 = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
CODE_LEN = getattr(settings, "SHORTENER_CODE_LEN", 7)
CODE_SPACE = len(BASE62) ** CODE_LEN  # so many

# not giving these to anyone
# thank u smart llm
RESERVED_WORDS = {
    "admin", "api", "accounts", "static", "healthz", "metrics",
    "profile", "stats", "favicon.ico", "robots.txt", "sitemap.xml",
    "raiden",
}

MAX_GEN_ATTEMPTS = 8
MISS_SENTINEL = "!"


class AliasConflict(Exception):
    pass


class ReservedAlias(Exception):
    pass


class UnsafeDestination(Exception):
    pass

_client = None
_client_lock = threading.Lock()


def get_redis():
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:
                # i love fakeredis
                url = getattr(settings, "REDIS_URL", "fakeredis://")
                if url.startswith("fakeredis://"):
                    import fakeredis

                    _client = fakeredis.FakeStrictRedis(decode_responses=True)
                else:
                    _client = redis_lib.Redis.from_url(url, decode_responses=True)
    return _client


def reset_redis(client=None):
    """Swap the client (used by tests)."""
    global _client
    _client = client

_lru = TTLCache(
    maxsize=getattr(settings, "SHORTENER_CACHE_LRU_SIZE", 1024),
    ttl=getattr(settings, "SHORTENER_CACHE_LRU_TTL", 60),
)
_lru_lock = threading.Lock()
_LRU_MISS = object()


def _lru_get(code):
    with _lru_lock:
        return _lru.get(code, _LRU_MISS)


def _lru_set(code, value):
    with _lru_lock:
        _lru[code] = value


def _lru_evict(code):
    with _lru_lock:
        _lru.pop(code, None)


def _url_cache_ttl():
    return getattr(settings, "SHORTENER_URL_CACHE_TTL", 300)


def generate_code() -> str:
    return "".join(secrets.choice(BASE62) for _ in range(CODE_LEN))


def validate_alias(alias: str) -> None:
    if not alias or len(alias) > 64:
        raise ReservedAlias("Alias must be 1-64 characters.")
    lowered = alias.lower()
    if lowered in RESERVED_WORDS:
        raise ReservedAlias(f"'{alias}' is reserved. No, we won't say why.")
    if not all(c in BASE62 + "-_" for c in alias):
        raise ReservedAlias("Alias may only contain letters, digits, '-' and '_'.")


def validate_destination(url: str, own_hosts: set[str]) -> None:
    """Reject destinations that could hurt us or our users."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise UnsafeDestination("Only http(s) URLs can be shortened.")
    host = parsed.hostname
    if not host:
        raise UnsafeDestination("URL has no host. funny.")
    host = host.lower()

    # Links that redirect back to us would loop forever.
    if host in own_hosts or any(host.endswith("." + h) for h in own_hosts):
        raise UnsafeDestination("nice try, but monke smart.")

    if not getattr(settings, "SHORTENER_STRICT_DESTINATION", True):
        return

    try:
        infos = socket.getaddrinfo(host, parsed.port or 443, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        raise UnsafeDestination("Destination host does not resolve.")
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_multicast
            or ip.is_reserved
            or ip.is_unspecified
        ):
            raise UnsafeDestination(
                "Destination resolves to a private/internal address. monke see SSRF. No fun."
            )


def create_link(origin: str, alias: str | None = None, ttl_seconds: int | None = None,
                owner=None) -> URLR:
    expires_at = None
    if ttl_seconds and ttl_seconds > 0:
        max_ttl = getattr(settings, "SHORTENER_MAX_TTL_SECONDS", 10 * 365 * 86400)
        expires_at = timezone.now() + timezone.timedelta(
            seconds=min(ttl_seconds, max_ttl)
        )
    elif getattr(settings, "SHORTENER_DEFAULT_TTL_SECONDS", 0):
        expires_at = timezone.now() + timezone.timedelta(
            seconds=settings.SHORTENER_DEFAULT_TTL_SECONDS
        )

    if alias:
        validate_alias(alias)
        if URLR.all_objects.filter(pk=alias).exists():
            raise AliasConflict("alias taken. pick another!")
        link = URLR(id=alias, origin=origin, expires_at=expires_at,
                    created_by=owner)
        try:
            with transaction.atomic():
                link.save()
        except IntegrityError:
            raise AliasConflict("alias taken. pick another!")
        return link

    # Random codes: retry on the (vanishingly rare) collision.
    for _ in range(MAX_GEN_ATTEMPTS):
        link = URLR(id=generate_code(), origin=origin, expires_at=expires_at,
                    created_by=owner)
        try:
            with transaction.atomic():
                link.save()
            return link
        except IntegrityError:
            continue
    raise RuntimeError("Could not allocate a unique short code.")



@dataclass
class Resolution:
    status: str  # ok/expired/deleted/missing
    origin: str | None = None


def _decode_cached(raw: str | None, code: str) -> Resolution | None:
    if raw is None:
        return None
    if raw == MISS_SENTINEL:
        return Resolution("missing")
    data = json.loads(raw)
    _lru_set(code, (data, timezone.now()))
    return _resolution_from_data(code, data)


def _resolution_from_data(code: str, data: dict) -> Resolution:
    if data.get("d"):
        return Resolution("deleted")
    exp = data.get("x")
    if exp and exp <= timezone.now().timestamp():
        return Resolution("expired")
    return Resolution("ok", origin=data["o"])


def resolve(code: str) -> Resolution:
    """LRU -> Redis -> Postgres. Populates caches as it goes."""
    hit = _lru_get(code)
    if hit is not _LRU_MISS:
        data, _ = hit if isinstance(hit, tuple) else (hit, None)
        if data is None:
            return Resolution("missing")
        return _resolution_from_data(code, data)

    try:
        raw = get_redis().get(f"url:{code}")
        res = _decode_cached(raw, code)
        if res is not None:
            return res
    except redis_lib.RedisError:
        log.warning("Redis unavailable on redirect path; falling through to DB")

    try:
        link = URLR.all_objects.get(pk=code)
    except URLR.DoesNotExist:
        try:
            get_redis().setex(f"url:{code}", 30, MISS_SENTINEL)
        except redis_lib.RedisError:
            pass
        _lru_set(code, (None, timezone.now()))
        return Resolution("missing")

    data = {
        "o": link.origin,
        "x": link.expires_at.timestamp() if link.expires_at else None,
        "d": link.deleted,
    }
    _lru_set(code, (data, timezone.now()))
    try:
        get_redis().setex(f"url:{code}", _url_cache_ttl(), json.dumps(data))
    except redis_lib.RedisError:
        pass
    if link.deleted:
        return Resolution("deleted")
    if link.is_expired():
        return Resolution("expired")
    return Resolution("ok", origin=link.origin)


def evict_code(code: str) -> None:
    _lru_evict(code)
    try:
        get_redis().delete(f"url:{code}")
    except redis_lib.RedisError:
        pass


def _counter_key(code: str) -> str:
    return f"cnt:{code}"


def _day_key(code: str, day: date) -> str:
    return f"day:{code}:{day.isoformat()}"


def _ref_key(code: str) -> str:
    return f"ref:{code}"


def record_click(code: str, referrer: str | None) -> None:
    today = timezone.now().date()
    ref = (referrer or "direct")[:250]
    r = get_redis()
    try:
        pipe = r.pipeline(transaction=True)
        pipe.incr(_counter_key(code))
        pipe.incr(_day_key(code, today))
        pipe.expire(_day_key(code, today), 86400)
        pipe.hincrby(_ref_key(code), ref, 1)
        pipe.expire(_ref_key(code), 86400)
        results = pipe.execute()

    except redis_lib.RedisError:
        log.warning("Redis broken, will just write to DB")
        try:
            URLR.all_objects.filter(pk=code).update(clicks=F("clicks") + 1)
        except Exception:
            log.exception("Click lost entirely for %s", code)
        return

    threshold = getattr(settings, "SHORTENER_CLICK_FLUSH_THRESHOLD", 100)
    if results[0] >= threshold:
        flush_link(code)


def _getdel(r, key) -> str | None:
    try:
        return r.getdel(key)
    except (AttributeError, redis_lib.ResponseError):
        with r.pipeline(transaction=True) as pipe:
            pipe.get(key)
            pipe.delete(key)
            return pipe.execute()[0]


def flush_link(code: str) -> None:
    r = get_redis()
    today = timezone.now().date()
    _flush_counter(r, code, _counter_key(code))
    _flush_daily(r, code, _day_key(code, today))
    _flush_referrers(r, code)


def _flush_counter(r, code: str, key: str) -> None:
    raw = _getdel(r, key)
    if raw:
        try:
            URLR.all_objects.filter(pk=code).update(
                clicks=F("clicks") + int(raw)
            )
        except Exception:
            log.exception("Failed to flush %s=%s for %s", key, raw, code)
            try:
                r.incrby(key, int(raw))  # put it back
            except redis_lib.RedisError:
                pass


def _flush_daily(r, code: str, key: str) -> None:
    raw = _getdel(r, key)
    if not raw:
        return
    day = date.fromisoformat(key.rsplit(":", 1)[-1])
    n = int(raw)
    try:
        with transaction.atomic():
            obj, created = DailyClick.objects.get_or_create(
                link_id=code, date=day, defaults={"count": 0}
            )
            DailyClick.objects.filter(pk=obj.pk).update(
                count=F("count") + n
            )
    except IntegrityError:
        DailyClick.objects.filter(link_id=code, date=day).update(
            count=F("count") + n
        )


def _flush_referrers(r, code: str) -> None:
    key = _ref_key(code)
    staging = f"{key}:flush:{secrets.token_hex(4)}"
    try:
        if not r.exists(key):
            return
        r.rename(key, staging)
        fields = r.hgetall(staging)
        r.delete(staging)
    except redis_lib.RedisError:
        return
    for name, n in fields.items():
        try:
            with transaction.atomic():
                obj, _ = Referrer.objects.get_or_create(
                    link_id=code, name=name, defaults={"count": 0}
                )
                Referrer.objects.filter(pk=obj.pk).update(
                    count=F("count") + int(n)
                )
        except IntegrityError:
            Referrer.objects.filter(link_id=code, name=name).update(
                count=F("count") + int(n)
            )


def flush_all_pending() -> dict:
    """Sweep every pending click/day/referrer key. Run every FLUSH_INTERVAL."""
    r = get_redis()
    flushed = {"links": 0, "days": 0, "refs": 0}
    for key in r.scan_iter(match="cnt:*"):
        code = key.split(":", 1)[1]
        _flush_counter(r, code, key)
        flushed["links"] += 1
    for key in r.scan_iter(match="day:*"):
        code = key.split(":")[1]
        _flush_daily(r, code, key)
        flushed["days"] += 1
    for key in r.scan_iter(match="ref:*"):
        if ":flush:" in key:
            continue
        code = key.split(":", 1)[1]
        _flush_referrers(r, code)
        flushed["refs"] += 1
    return flushed


def pending_clicks(code: str) -> int:
    """Clicks sitting in the Redis buffer, not yet flushed to Postgres."""
    try:
        raw = get_redis().get(_counter_key(code))
        return int(raw) if raw else 0
    except (redis_lib.RedisError, ValueError):
        return 0


def check_rate_limit(ip: str) -> bool:
    """True if the request may proceed. Configurable window + count."""
    limit = getattr(settings, "SHORTENER_RATE_LIMIT", 5)
    window = getattr(settings, "SHORTENER_RATE_WINDOW", 60)
    key = f"rl:{ip}"
    try:
        r = get_redis()
        n = r.incr(key)
        if n == 1:
            r.expire(key, window)
        return n <= limit
    except redis_lib.RedisError:
        # Fail open: a dead cache shouldn't take down link creation.
        log.warning("Redis down; allowing create request through")
        return True
