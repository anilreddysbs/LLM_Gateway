# LLM Gateway: Architecture, Codebase Walkthrough & Learning Guide

Welcome to the internal engineering guide for the **LLM Gateway**. This guide is written for engineers looking to understand the mechanics of building a production-grade AI proxy.

---

## 1. Recommended Codebase Reading Order

To understand how a request flows through the gateway from edge to upstream and back to the client, read the files in the following order:

1. **`config/models.yaml` & `src/llm_gateway/config/models_config.py`**
   - Learn how models, providers, USD pricing per million tokens, and fallback alias chains (`default`, `fast`, `quality`) are defined and parsed with `Decimal` precision.
2. **`src/llm_gateway/db/models.py` & `src/llm_gateway/core/auth.py`**
   - Understand API key generation (`gw-live-...`), constant-time hash verification (`hmac.compare_digest`), and 60-second Redis authentication caching.
3. **`src/llm_gateway/core/rate_limit.py`**
   - Review the atomic Redis Lua script enforcing sliding-window rate limiting (RPM) and monthly USD spend budget limits.
4. **`src/llm_gateway/core/cache.py`**
   - Study canonical JSON request normalization, exact-match SHA-256 caching, cache eligibility rules (`temperature <= 0.3`), and distributed stampede locks (`SET key val NX EX`).
5. **`src/llm_gateway/core/resilience/`**
   - `backoff.py`: Exponential backoff with AWS Full Jitter calculation and `Retry-After` header extraction.
   - `circuit_breaker.py`: Distributed 3-state circuit breaker (`CLOSED`, `OPEN`, `HALF_OPEN`) tracking failure thresholds and cooldown windows in Redis.
   - `fallback.py`: Traversal across alias targets, combining retries, circuit breaking, and failover.
   - `streaming.py`: SSE generator implementing the *fallback-before-first-chunk* guarantee.
6. **`src/llm_gateway/providers/`**
   - `base.py`: Provider interface contracts (`complete()` and `stream()`).
   - `gemini.py`: Google Gemini adapter handling chat messages and streaming chunks.
   - `openai_compat.py`: Standard OpenAI-compatible client adapter.
7. **`src/llm_gateway/observability/`**
   - `background.py`: Non-blocking in-process `asyncio.Queue` with batched persistence to PostgreSQL.
   - `metrics.py`: Prometheus metrics (counters, gauges, histograms).
   - `logging.py`: Structured JSON logger with `request_id` context propagation.
8. **`src/llm_gateway/api/v1/chat.py` & `src/llm_gateway/main.py`**
   - The master endpoint tying routing, auth, rate limiting, caching, fallback, and logging together within FastAPI lifespan management.

---

## 2. Deep-Dive Module Walkthrough

### Request Journey Lifecycle

```
[Client] 
   │  POST /v1/chat/completions (with Bearer gw-live-...)
   ▼
[RequestContextMiddleware]
   │  Assigns or propagates X-Request-ID (ContextVar)
   ▼
[verify_client_api_key]
   │  Checks Redis cache (60s TTL) -> DB fallback -> Constant-time SHA-256 compare
   ▼
[check_monthly_budget]
   │  Checks current month spend in Redis / DB against api_key.monthly_budget_usd
   ▼
[SlidingWindowRateLimiter]
   │  Runs atomic Redis Lua script -> returns allowed / 429 Retry-After
   ▼
[RequestRouter.resolve(model)]
   │  Resolves alias (e.g. "default") into RoutingTargets [gemini, openai]
   ▼
[ResponseCache.get_or_compute]
   │  If temperature <= 0.3 and not streaming:
   │    Computes canonical SHA-256 hash
   │    Checks Redis cache -> if HIT, returns cached response (0 USD, <2ms)
   ▼ (on MISS)
[FallbackExecutor.execute]
   │  Checks CircuitBreaker.can_execute() for primary target
   │  Calls Primary Provider with timeout and AWS Full Jitter retries
   │  On persistent failure -> trips CircuitBreaker -> hops to Secondary Provider
   ▼
[UsageLogQueue.enqueue]
   │  Non-blocking enqueue of request telemetry and Decimal USD cost to background queue
   ▼
[Prometheus Metrics & Gateway Headers]
   │  Updates counters/histograms; returns X-Gateway-Cache, X-Gateway-Provider, X-RateLimit-*
   ▼
[Client Response]
```

---

## 3. 15 Self-Test Questions & Answers

### Q1: Why do we use `hmac.compare_digest` instead of `==` for API key verification?
**A:** Standard string equality (`==`) terminates comparison at the first mismatched byte, leaking timing information proportional to the length of the matching prefix. `hmac.compare_digest` executes in constant time, neutralizing timing side-channel attacks.

### Q2: Why is the sliding window implemented in Redis Lua instead of separate Redis commands?
**A:** If executed as separate commands (`ZREMRANGEBYSCORE`, `ZCARD`, `ZADD`), a race condition occurs between concurrent workers checking the count and adding their entry, allowing traffic bursts to exceed rate limits. Lua scripts execute atomically in Redis, eliminating race conditions without distributed locks.

### Q3: How does the rate limiter compute the exact `Retry-After` header when limit is exceeded?
**A:** It calls `redis.call('ZRANGE', key, 0, 0, 'WITHSCORES')` to get the timestamp of the oldest entry in the window. The seconds until that entry expires `(oldest_time + window_ms - now_ms) / 1000` is the exact time when quota will become available.

### Q4: Why is caching restricted to `temperature <= 0.3` by default?
**A:** At higher temperatures (e.g., 0.7 to 1.0), users expect stochastic, creative variations in output. Caching high-temperature completions violates user expectations and breaks non-deterministic workflows.

### Q5: What is cache stampede (thundering herd) and how does the gateway prevent it?
**A:** Cache stampede occurs when an un-cached or expired hot key receives hundreds of concurrent requests simultaneously, causing all workers to call upstream LLMs at once. The gateway uses an atomic Redis lock (`SET lock_key uuid NX EX 10`). The first worker computes and caches the result; concurrent workers wait and poll Redis, receiving the newly cached response without duplicate upstream calls.

### Q6: What is the fallback-before-first-chunk guarantee in SSE streaming?
**A:** A streaming connection can safely fallback to a secondary provider *only* before any bytes have been sent to the client. Once the first chunk is sent, HTTP headers are locked and output tokens are partially rendered. Falling back mid-stream would concatenate incompatible responses from different models. Therefore, mid-stream errors must terminate with an SSE error event (`stream_interrupted`) and `[DONE]`.

### Q7: What are the three states of the Redis Circuit Breaker and how do they transition?
**A:**
1. `CLOSED`: Normal operation; requests pass through.
2. `OPEN`: Tripped after $N$ consecutive failures (default 5). All requests bypass or fail fast for a cooldown period (default 30s).
3. `HALF_OPEN`: Cooldown elapsed. Allows a limited number of probe requests (default 1) to test upstream health. A successful probe resets to `CLOSED`; a failure re-trips to `OPEN`.

### Q8: Why do we use AWS Full Jitter backoff instead of plain exponential backoff?
**A:** Plain exponential backoff causes retrying clients to synchronize in waves, producing periodic traffic spikes ("retry storms") on upstream providers. AWS Full Jitter (`sleep = random.uniform(0, min(max_backoff, base * 2^attempt))`) disperses retries evenly across time.

### Q9: Why is `Decimal` used instead of `float` for token cost calculations?
**A:** IEEE 754 floating-point arithmetic produces precision errors (e.g., `0.1 + 0.2 = 0.30000000000000004`). In financial accounting and multi-tenant budget metering across millions of micro-transactions, floating-point drift creates billing discrepancies. Python's `Decimal` guarantees exact arithmetic.

### Q10: How does tenant cache isolation work?
**A:** When an API key has `isolate_cache=True`, its API key ID is incorporated into the cache key hash. This prevents tenant A from ever observing or retrieving completions generated by tenant B.

### Q11: What happens if the `UsageLogQueue` buffer becomes completely full?
**A:** It drops the record, logs an error, and increments the `DROPPED_LOGS_TOTAL` Prometheus metric. The gateway prioritizes serving user inference requests over audit log completeness during catastrophic database degradation.

### Q12: Why is the `request_id` stored in a `ContextVar`?
**A:** In asynchronous Python (`asyncio`), thread-local storage fails because multiple concurrent coroutines execute on the same OS thread. Python's `contextvars.ContextVar` isolates context per coroutine execution tree, allowing request IDs to be accessed anywhere without passing them through every function parameter.

### Q13: What is the difference between `Cache-Control: no-cache` and `no-store`?
**A:** `no-cache` instructs the gateway to bypass reading from cache (forcing an upstream call), but permits caching the fresh response. `no-store` instructs the gateway to completely refrain from storing the response in the cache.

### Q14: How does graceful shutdown prevent data loss in the usage log queue?
**A:** In `main.py` lifespan shutdown, `await usage_queue.stop()` is invoked. The queue worker stops accepting new items, cancels the background loop, and executes `await self.flush()`, committing all remaining queued records to Postgres before the database pool disposes.

### Q15: Which HTTP errors are considered retriable versus fail-fast?
**A:**
- **Retriable:** Network timeouts, HTTP 429 (Rate Limit / Quota), and HTTP 5xx (500, 502, 503, 504).
- **Fail-Fast (Non-Retriable):** HTTP 400 (Bad Request), 401 (Unauthorized), 403 (Forbidden), 404 (Not Found), 422 (Unprocessable Content). Retrying a malformed request will never succeed and wastes time and budget.
