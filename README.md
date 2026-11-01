# monkeurl

A production-minded URL shortener. Django + DRF + Postgres + Redis + Gunicorn,
fully dockerized. Every link deserves a home; the bad ones get sent back.

(Yes, I've used LLMs in this project to write some of the docs and code)

## Architecture

```
            create (rate-limited, abuse-checked)
  client ─────────────────────────────┐
    │                                 ▼
    │  GET /{code}/            API views / services
    └─────────► resolve():  LRU (per-worker, TTL)
                               │ miss
                               ▼
                            Redis  url:{code}
                               │ miss
                               ▼
                          Postgres ────────┐
                                           │
    click: INCR cnt:{code} (Redis)         │
           day:{code}:{date}               │
           ref:{code} (hash)               │
                │  ≥100 clicks inline      │ flush
                │  or every FLUSH_INTERVAL ▼
            flush_clicks daemon ──► URLR.clicks (F() += δ)
                                    DailyClick / Referrer
```

- **Two cache layers** on the read path: a per-worker `cachetools.TTLCache`
  (bounded LRU, configurable size + TTL) in front of a Redis JSON blob with
  its own TTL, in front of Postgres. Negative results are cached too (short
  TTL) so 404 floods don't hit the DB.
- **Write-behind click tracking**: redirects `INCR` Redis counters; counts
  flush to Postgres when a link reaches `CLICK_FLUSH_THRESHOLD` or when the
  `flusher` container sweeps every `FLUSH_INTERVAL` seconds. Stats endpoints
  report `stored + pending` so numbers are always current.
- **Soft delete** with two managers: `URLR.objects` hides deleted links,
  `URLR.all_objects` sees everything (used by resolve so a deleted link is a
  correct 410, not a misleading 404, and so deleted aliases stay burned).

## Feature behaviour & failure cases

| Feature | Normal | Failure behaviour |
|---|---|---|
| Shorten | `POST /api/links/` `{origin}` → 201 + 7-char base62 code | invalid URL → 400 |
| Custom alias | `{alias}` → used verbatim | taken → 409; reserved/bad chars → 400 |
| Reserved words | `admin`, `api`, `milhouse`, ... → 400 | list in `services.RESERVED_WORDS` |
| Expiry | `{ttl_seconds}` → 410 after deadline | expired → 410 Gone, not counted as click |
| Rate limit | per-IP `RATE_LIMIT` per `RATE_WINDOW` (Redis) | exceeded → 429 with the actual limit stated |
| Self-redirect | origin host ∈ own hosts → 400 | also DNS-resolves and blocks private/loopback/link-local/reserved IPs → 400 |
| Unsafe schemes | only `http`/`https` | `javascript:`/`ftp:` → 400 |
| Delete | owner or staff only → 204 | anonymous-owned links → 403; stranger → 403; no edit endpoint exists (405) |
| Redis down | redirects fall through to DB; clicks write directly to Postgres | create stays up (rate limiter fails open) |
| DB down | cached hot links still redirect; creates fail 500 | healthz reports `"dying"` |

## Configuration (env vars, all of it)

Everything is an env var; `config.toml` is a dev-only fallback and is
`.dockerignore`d out of the image. See `.env.example` — the important ones:

| Var | Default | Meaning |
|---|---|---|
| `SHORTENER_RATE_LIMIT` / `SHORTENER_RATE_WINDOW` | `5` / `60` | creates per IP per window |
| `SHORTENER_CODE_LEN` | `7` | base62 code length (62⁷ ≈ 3.5 trillion) |
| `SHORTENER_CACHE_LRU_SIZE` / `SHORTENER_CACHE_LRU_TTL` | `1024` / `60` | per-worker LRU |
| `SHORTENER_URL_CACHE_TTL` | `300` | Redis url→code cache TTL |
| `SHORTENER_CLICK_FLUSH_THRESHOLD` | `100` | clicks buffered before inline flush |
| `SHORTENER_FLUSH_INTERVAL` | `300` | daemon sweep interval (seconds) |
| `SHORTENER_DEFAULT_TTL_SECONDS` | `0` | default link lifetime (0 = never) |
| `SHORTENER_REDIRECT_STATUS` | `301` | 301 caches in browsers (fewer hits, fewer counted clicks); set `302` for exact analytics |
| `SHORTENER_STRICT_DESTINATION` | `true` | DNS-resolve + block private IPs |
| `SHORTENER_OWN_HOSTS` | from `SITE_HOST` | hostnames links may not point at |
| `DATABASE_URL`, `REDIS_URL`, `SECRET_KEY`, `DEBUG`, `ALLOWED_HOSTS` | — | the usuals |

## Local dev (uv, no Docker needed)

```bash
uv sync                                  # creates .venv, installs deps
uv run python manage.py migrate
uv run python manage.py runserver        # http://localhost:8000
```

Bare local dev runs fine on SQLite with no Redis — the code degrades
gracefully (fail-open limiter, direct-write clicks). For the real path:
`REDIS_URL=redis://127.0.0.1:6379/0 uv run python manage.py runserver`.

## Docker (the real deployment)

```bash
cp .env.example .env          # set SECRET_KEY at minimum
docker-compose up --build -d
curl http://localhost:8000/healthz
```

Services: `web` (gunicorn, 4 workers × 2 threads), `db` (postgres:16, volume
`pgdata`), `redis` (appendonly, volume `redisdata`), `flusher` (the
click-flush daemon). Persistent storage is the two named volumes — data
survives `docker-compose down` and only dies on `down -v`.

To prove persistence: `docker-compose restart web` — links still redirect;
only in-flight Redis click buffers for the last ≤ FLUSH_INTERVAL seconds are
at risk (mitigated by `appendonly`).

## Tests

```bash
uv run python manage.py test tests            # 45 tests
uv run python manage.py test tests -v 2       # watch the screenplay
```

Coverage: end-to-end flow, failure cases per feature (alias conflict,
reserved words, bad schemes, private IPs, self-redirect, expiry, rate limit,
ownership), in-process concurrency (24-way alias race, click storm, unique
codes), persistence across simulated restart, cache layers + invalidation +
Redis-outage degradation.

## Load test (the 200-concurrent-requests part)

```bash
python3 scripts/load_test.py --base-url http://localhost:8000 --workers 200
```

Real output captured in `docs/load_test_results.txt`. Summary of the run:

```
alias race:   200 workers, same alias  -> 1×201, 199×409, exactly one row
creates:      200/200 unique codes
click storm:  600 redirects -> flushed clicks=600, pending=0
rate limit:   shared-IP burst -> {201: 5, 429: 5} at limit 5/60s
cold vs warm: p50 1.3ms vs 0.9ms (~1.5×); warm path = 0 DB queries
```

Safeguards that make this true: the PK unique constraint is the final arbiter
for aliases (losers get `IntegrityError` → 409); random codes retry on
collision; clicks are atomic `INCR`s flushed with `GETDEL`/`RENAME` so a
delta is claimed by exactly one flusher; DB counters move with `F()` so
flushers can't lost-update each other.

## API

| Method | Path | Body | Result |
|---|---|---|---|
| POST | `/api/links/` | `{origin, alias?, ttl_seconds?}` | 201 link JSON |
| GET | `/api/links/{code}/` | — | stats JSON (clicks = stored+pending, daily, referrers) |
| DELETE | `/api/links/{code}/` | — | 204 owner/staff; 403 otherwise |
| GET | `/{code}/` | — | 301 → origin; 404/410 otherwise |
| GET | `/stats/{code}/` | — | HTML stats + QR |
| GET | `/healthz` | — | liveness JSON |

## Known limitations (honest list)

- **301 by default** trades exact analytics for fewer hits — repeat visits
  from one browser never reach us. Set `SHORTENER_REDIRECT_STATUS=302`.
- **Rate limiting is per-IP via X-Forwarded-For.** Without a trusted proxy
  stripping client-supplied XFF, it's spoofable (the load test does exactly
  this, on purpose, to demonstrate per-IP behaviour).
- Each gunicorn worker has its own LRU — a fresh-delete can serve one stale
  redirect for up to `CACHE_LRU_TTL` (default 60s) on the unlucky worker;
  Redis layer and the DB path are exact.
- Alias creation and redirects share the app's concurrency model; Postgres
  `select_for_update` isn't needed because uniqueness is enforced by the PK,
  not by check-then-insert.
- The flusher is a single process; if it dies, buffered clicks sit in Redis
  (safe) and stats still report stored+pending correctly.
