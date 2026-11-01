# monkeurl — Design Document

*Written as a pre-mortem: we assume the grader's 200-concurrent-request load
test already happened, and this document explains why the system survived it.*

---

## 1. The incident we designed for

At grading time, an external actor initiates 200 simultaneous requests: a
contested alias race, a click storm, and a burst of same-IP creates. We
prepared for this day. Every design decision below exists because some
version of that incident has to either succeed, fail correctly, or be
provably impossible.

## 2. Short-code generation and uniqueness across instances

**Implemented.** Codes are 7 base62 characters drawn from
`secrets` (CSPRNG), so `62^7 ≈ 3.52 × 10^12` possible codes. Generation is
**stateless**: any number of application instances can mint codes without
coordination because correctness does not live in the generator — it lives
in the Postgres primary key constraint. `create_link` inserts inside a
transaction; on `IntegrityError` it retries up to 8 times. The probability
of even needing a retry is governed by the birthday bound: at 10 million
stored codes, the per-insert collision chance is ≈ 0.14%; at 100 million,
≈ 1.4%. The retry budget is sized so exhaustion is effectively impossible.

Custom aliases take the same path: a cheap `EXISTS` check for a friendly 409
message, then the PK constraint as the real arbiter. The load test proves
this: 200 workers, one alias, exactly one `201`, 199 `409`s, one row.

**Not implemented (larger deployments).** Stateless generation scales
horizontally *by construction* until the birthday bound gets uncomfortable
(~hundreds of millions of links at 7 chars). At that point: lengthen codes,
or switch to a sharded ID allocator (per-instance range blocks à la Flickr's
ticket servers / Snowflake-style IDs). Coordination-free generation was the
point; a central code service would be a regression, not an evolution.

## 3. Concurrent writes and click tracking

**Implemented.** Three atomicity layers, each chosen so no lock is needed:

- **Clicks**: `INCR` on Redis `cnt:{code}` — single-threaded, atomic.
  Redirects never write Postgres, so the hot path can't lost-update.
- **Flush**: the pending delta is claimed with `GETDEL` (atomic) or the
  staging-`RENAME` trick for hashes — at most one flusher can ever own a
  delta. DB application is `UPDATE ... SET clicks = clicks + δ` via `F()`,
  so concurrent flushers compose commutatively.
- **Aliases**: PK constraint + transaction (see §2). `select_for_update` is
  deliberately absent — there is no read-modify-write to protect.

Verified by `test_click_storm_counts_every_click_exactly_once` (asserts the
buffered sum lands exactly once) and the load-test flush phase
(`clicks=600, pending=0`).

**Trade-off.** Redis is an uncommitted buffer: a hard Redis crash between
sweeps loses ≤ one interval of clicks (mitigated by AOF + `appendonly`).
Acceptable for analytics; not acceptable for billing. Larger deployments
would write clicks to a durable log (Kafka) and aggregate downstream —
the Redis buffer becomes a performance optimization, not the ledger.

## 4. When the database becomes the bottleneck

Reads first: redirects are expected to dominate, and the read path is
LRU → Redis → Postgres with negative caching — Postgres only sees cold
misses and writes. If it still saturates: (1) Postgres read replicas behind
`DATABASE_ROUTERS` — resolution reads replica, writes hit primary;
(2) connection pooling via PgBouncer in front of N workers;
(3) partition `dailyc`/`referrer`-style tables by date if analytics grows.

Writes second: creates are rare relative to redirects, but a sustained write
flood has no queue in the current design — Postgres backpressures directly
into gunicorn timeouts. At scale: an insert queue (or batched COPY) for
analytics writes, and the PK constraint stays the correctness layer even
when sharded.

## 5. Reliability: caching, database, deployment choices

- **Redis over locmem** — locmem can't be shared across gunicorn workers;
  the rate limiter would silently multiply by worker count. This is the
  classic "it worked on my laptop" bug and the reason Redis is mandatory in
  the compose file, not optional.
- **Two managers, soft delete** — deletes keep referential integrity and let
  deleted aliases stay burned (re-issuing a dead alias would resurrect stale
  inbound links at someone else's destination).
- **Health endpoint** distinguishes liveness from readiness (`db`/`redis`
  fields), so orchestrators can drain a wedged instance.
- **Graceful degradation** — Redis down ⇒ fail-open rate limiting,
  direct-to-Postgres clicks, LRU still serving. DB down ⇒ cached hot links
  still redirect. Every degradation is tested (`test_redis_outage_*`).

## 6. Beyond a single instance

Current compose = 1 web. Scaling path, roughly in order:

1. **More gunicorn workers/threads** — CPU-bound headroom on one box.
2. **N × web + one flusher behind a load balancer** — works *today* with no
   code changes: codegen is stateless, Redis is shared, flusher deltas are
   claim-once. Per-worker LRUs can diverge for ≤ `LRU_TTL` seconds; that's
   the documented staleness bound.
3. **Managed Postgres (RDS-class)** — replication/failover/faster disks out
   of the box; app just repoints `DATABASE_URL`.
4. **CDN/edge redirects** — `301`s are aggressively cacheable; a CDN in
   front could serve hot redirects without touching us at all, which is
   the endgame for read-heavy shorteners.
5. **A real task queue** (Celery/SQS) for the flusher so sweep work scales
   and retries.

## 7. Honest trade-offs and what I'd fix with more time

- **301 vs analytics** — browsers cache the redirect, so we trade exact
  click counts for server offload. `SHORTENER_REDIRECT_STATUS=302` flips it;
  a smarter version does `302` for analytics links, `301` for bulk traffic.
- **Per-worker LRU staleness** — deletion is exact through Redis and DB but
  a stale LRU entry can serve one redirect for ≤ 60s on an unlucky worker.
  With more time: pub/sub invalidation across workers, or kill the LRU and
  let Redis carry the hot set (it already does).
- **XFF trust** — the limiter keys on `X-Forwarded-For`, which is spoofable
  without a proxy stripping it. Real deployment: trusted proxy sets the
  header; app ignores client-supplied values.
- **Allauth + Google OAuth for ownership** — works, but no credential
  rotation story and no rate-limit-per-account fallback. More time:
  per-account quotas layered under per-IP ones.
- **Flusher is a singleton** — designed to be safe as N (claim-once deltas),
  but only ever run as 1. More time: lease-based leader election, or just
  run 2 and let the atomicity make them harmless.
- **Single-flight DNS validation** — `getaddrinfo` in the request path adds
  latency to creates and can be fooled by DNS rebinding. More time:
  async validation queue + re-resolve before first redirect.
