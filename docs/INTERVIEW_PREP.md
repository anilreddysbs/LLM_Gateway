# LLM Gateway: Production Engineering Interview Preparation Guide

This guide contains 25 deep production engineering interview questions with direct, codebase-grounded answers designed to demonstrate mastery of high-throughput distributed systems, resilience patterns, and AI infrastructure.

---

### 1. High-Level Architecture & API Design
**Q: How would you design an enterprise LLM gateway that proxies multiple upstream AI providers?**  
**A:** A modular gateway architecture separating the control plane (key provisioning, tenant budgeting, routing aliases) from the data plane (request normalization, rate limiting, caching, fallback execution, and streaming). In our codebase:
- Edge layer: FastAPI ASGI application with structured logging and `ContextVar` request ID propagation.
- Policy layer: Redis Lua sliding window rate limiter and monthly budget guard.
- Cache layer: Deterministic SHA-256 exact-match cache with distributed stampede protection.
- Execution layer: FallbackExecutor orchestrating exponential backoff with AWS Full Jitter and a 3-state Redis Circuit Breaker.
- Upstream adapters: Polymorphic `BaseProvider` implementations mapping to standard OpenAI-compatible chunks and responses.
- Observability: Prometheus scrape endpoint (`/metrics`) and asynchronous in-process queue batch-flushing audit logs to Postgres.

---

### 2. Rate Limiting Concurrency
**Q: Why choose a Sliding Window Counter over a Token Bucket or Leaky Bucket for LLM APIs?**  
**A:** Upstream LLM providers (e.g. OpenAI/Anthropic) enforce strict rolling 60-second quotas. Fixed-window and leaky bucket algorithms fail to prevent 2x boundary spikes across rolling windows. While Token Bucket is excellent for smoothing network packet flows, Sliding Window with Redis Sorted Sets (`ZSET`) provides strict rolling window enforcement and allows exact calculation of `Retry-After` seconds using the oldest timestamp score in the sorted set.

---

### 3. Redis Lua Concurrency
**Q: Why must rate limiting be executed via a Redis Lua script instead of standard Python async calls?**  
**A:** Multi-step Redis operations (`ZREMRANGEBYSCORE` -> `ZCARD` -> `ZADD`) executed from Python involve network round-trips where interleaving concurrent requests on different worker nodes can both see `count = limit - 1` and proceed, violating the rate limit ceiling. Redis executes Lua scripts as a single atomic transaction on the Redis main thread without distributed lock overhead.

---

### 4. Cache Stampede Prevention
**Q: How do you prevent a "thundering herd" or "cache stampede" when a popular cache entry expires under 500 RPS?**  
**A:** In `src/llm_gateway/core/cache.py`, we implement distributed lock stampede protection using Redis `SET lock_key uuid NX EX 10`. When a cache miss occurs:
1. The first request acquires the lock and initiates the upstream LLM call.
2. Concurrent requests fail to acquire the lock and enter a backoff polling loop (50ms intervals).
3. Once the first request completes and caches the result, waiting requests fetch the cached value directly from Redis without issuing duplicate upstream requests.

---

### 5. Deterministic Cache Keys
**Q: How do you ensure cache key consistency when different JSON serialization libraries order dictionary keys differently?**  
**A:** In `normalize_request_for_cache()`, we extract request parameters, sort message keys, and serialize using `json.dumps(..., sort_keys=True, separators=(',', ':'))`. This guarantees that `{"model": "gpt-4o", "temperature": 0.2}` and `{"temperature": 0.2, "model": "gpt-4o"}` produce byte-for-byte identical SHA-256 hashes.

---

### 6. Streaming Fallback Invariants
**Q: Can you fallback to another provider if an error occurs while streaming a response? Why or why not?**  
**A:** Fallback is strictly permitted **only before the first chunk has been transmitted over the wire**. Once the first chunk is sent:
1. The client has received HTTP 200 and initial response bytes.
2. Mid-stream failover would blend outputs from two different models with disparate voices, formats, or contexts.
3. Therefore, errors after chunk 0 must emit an SSE error event (`stream_interrupted`) and close with `[DONE]`.

---

### 7. Circuit Breaker Mechanics
**Q: Describe the state transitions of the distributed circuit breaker in `circuit_breaker.py`.**  
**A:**
- `CLOSED -> OPEN`: Tripped when consecutive 5xx/timeout failures reach the threshold (e.g. 5) within the failure window. Cooldown timer (e.g. 30s) is recorded in Redis.
- `OPEN -> HALF_OPEN`: When a request arrives after the cooldown timestamp has elapsed.
- `HALF_OPEN -> CLOSED`: When a probe request succeeds, Redis failure and probe keys are deleted.
- `HALF_OPEN -> OPEN`: When a probe request fails, resetting the cooldown timer.

---

### 8. Backoff and Jitter
**Q: Why is Full Jitter superior to Equal Jitter or pure Exponential Backoff?**  
**A:** Pure exponential backoff results in synchronized retries ("retry thundering herds"). Equal Jitter keeps a deterministic floor. AWS Full Jitter (`sleep = uniform(0, min(max_backoff, base * 2^attempt))`) maximizes entropy and completely desynchronizes clients, achieving optimal upstream queue draining and minimal contention.

---

### 9. Asynchronous In-Process Logging vs External Message Queues
**Q: When would you use an in-process `asyncio.Queue` over Kafka/RabbitMQ for audit logs?**  
**A:** For a single-cluster or containerized API gateway, introducing Kafka or RabbitMQ adds significant operational complexity, serialization latency, and infrastructure cost. An in-process `asyncio.Queue` provides non-blocking sub-millisecond enqueuing with batch database transactions, satisfying 99.9% of gateway audit requirements. If horizontal node crashes are an unacceptable risk for financial compliance, the interface can be swapped for a durable external queue.

---

### 10. Financial Precision in AI Billing
**Q: Why is `Decimal` mandatory for token cost accounting?**  
**A:** LLM prices are fractions of a cent (e.g., $0.15 per 1M tokens = $0.00000015 per token). Over millions of requests, binary floating-point representation errors accumulate into substantial financial variances. Python `Decimal` with quantized rounding preserves exact accounting integrity.

---

### 11. Constant-Time Authentication
**Q: What vulnerability does `hmac.compare_digest` mitigate?**  
**A:** Standard string comparison operators (`==`) exit early on the first mismatched character. Attackers can measure execution time differences with microsecond accuracy to guess API keys character by character. `hmac.compare_digest` evaluates all bytes in constant time, neutralizing timing side-channel attacks.

---

### 12. Graceful Shutdown Implementation
**Q: What must happen when a gateway process receives a SIGTERM signal?**  
**A:**
1. Stop accepting new traffic (load balancer health checks fail).
2. Allow active in-flight requests to complete within a grace timeout.
3. Stop the background log queue consumer and flush all remaining queued records to the database.
4. Close Redis client connection pools and dispose the SQLAlchemy async engine.

---

### 13. Tenant Cache Isolation
**Q: What is a multi-tenant cache poisoning attack and how do you prevent it?**  
**A:** If multiple tenants share a cache, Tenant A could query a prompt containing sensitive data or malicious context that is subsequently served to Tenant B. Setting `isolate_cache=True` hashes the client's `api_key_id` into the cache key namespace (`f"{api_key_id}:{cache_hash}"`), guaranteeing cryptographic partition between tenants.

---

### 14. Observability: Key Prometheus Metrics
**Q: Which 5 metrics would you alert on for an LLM Gateway?**  
**A:**
1. `llm_gateway_requests_total` partitioned by status code (detecting 5xx spikes).
2. `llm_gateway_request_duration_seconds` p95/p99 latency histogram (detecting upstream degradation).
3. `llm_gateway_circuit_breaker_state == 2` (alerting when an upstream model has tripped).
4. `llm_gateway_rate_limit_exceeded_total` (identifying tenants hitting capacity limits).
5. `llm_gateway_dropped_logs_total` (alerting when the background queue is saturated).

---

### 15. ContextVar in Async Python
**Q: Why can't we use thread-local storage for request IDs in FastAPI?**  
**A:** FastAPI runs on `asyncio`. An event loop runs many concurrent requests as coroutines on a single OS thread. Thread-local storage would leak variables across different requests. `contextvars.ContextVar` isolates values per coroutine execution context.

---

### 16. Upstream Timeout Budgeting
**Q: How do you handle request deadlines when traversing a fallback chain?**  
**A:** We define an overall request deadline (`now + REQUEST_TIMEOUT_SECONDS`). For each attempt on a target, the per-attempt timeout is `min(PROVIDER_ATTEMPT_TIMEOUT_SECONDS, deadline - now)`. If the overall deadline is reached, we abort immediately rather than starting new attempts that are guaranteed to exceed the client's timeout.

---

### 17. Fail-Open vs Fail-Closed
**Q: In what scenarios should a gateway rate limiter fail-open vs fail-closed?**  
**A:**
- **Fail-Open:** High-traffic consumer applications where user experience is prioritized over strict quota enforcement during an internal infrastructure incident (e.g., Redis restart).
- **Fail-Closed:** B2B paid SaaS APIs where unpaid usage directly incurs high upstream token costs that cannot be absorbed.

---

### 18. Estimating Streaming Tokens
**Q: Upstream SSE streams don't always provide usage chunks. How do you account for streaming cost?**  
**A:** If the upstream provider yields explicit token counts on the finish chunk, use them. Otherwise, compute an estimate based on character counts: `input_tokens = len(messages_text) // 4`, `output_tokens = len(collected_text) // 4`, and flag the log record with `estimated_tokens = True`.

---

### 19. Handling Provider 429 vs Upstream 5xx
**Q: How should the gateway treat upstream 429 vs upstream 503?**  
**A:** Both are retriable errors. However, upstream 429 often provides a `Retry-After` header. If present, the gateway honors `Retry-After` rather than using standard exponential backoff. If repeated retries fail, both contribute to tripping the circuit breaker.

---

### 20. Database Connection Pooling under High Concurrency
**Q: How do you configure SQLAlchemy async connection pools for high-throughput APIs?**  
**A:** Use `asyncpg` with `pool_size=20`, `max_overflow=10`, and `pool_pre_ping=True`. `pool_pre_ping` validates connection liveliness before checking out, preventing stale socket errors following database maintenance or idle timeouts.

---

### 21. Preserving Upstream Provider Headers
**Q: What telemetry headers should the gateway add to client responses?**  
**A:**
- `X-Request-ID`: Trace identifier for log correlation.
- `X-Gateway-Provider`: The actual upstream provider that served the request (e.g. `gemini` or `mock`).
- `X-Gateway-Cache`: `HIT` or `MISS`.
- `X-Gateway-Fallback`: `true` if a fallback provider was utilized.
- `X-RateLimit-Limit`, `X-RateLimit-Remaining`, `X-RateLimit-Reset`: Client quota visibility.

---

### 22. SQL Percentile Calculation in SQLite vs PostgreSQL
**Q: How do you compute p50, p95, and p99 latencies efficiently without overloading Postgres?**  
**A:** In PostgreSQL, use `percentile_cont(0.50) WITHIN GROUP (ORDER BY latency_ms)`. For portability and testing across SQLite and Postgres, we fetch the sorted list of latencies for the target window and calculate percentiles in Python memory using linear interpolation.

---

### 23. Avoiding Circular Fallback Loops
**Q: How do you ensure fallback chains don't loop infinitely?**  
**A:** Routing targets in `models.yaml` are strictly ordered, directed acyclic lists (e.g. `[mock-primary, mock-secondary, mock-fallback]`). The executor iterates linearly across indices; once an index is exhausted, it advances forward and never backtracks.

---

### 24. Pydantic v2 Performance Benefits
**Q: Why standardize on Pydantic v2 for an API Gateway?**  
**A:** Pydantic v2 core logic is implemented in Rust (`pydantic-core`), delivering a 5x–20x performance improvement over v1 in parsing, serialization, and validation. In a gateway proxying thousands of JSON requests per second, serializing and validating payloads in Rust eliminates Python interpreter overhead.

---

### 25. High Availability Architecture Strategy
**Q: How does this gateway scale to handle 50,000 RPS?**  
**A:**
1. Gateway nodes are stateless: scale horizontally behind a Layer 7 Load Balancer (e.g. AWS ALB, NGINX, Cloudflare).
2. Shared state (rate limits, cache, circuit breakers) is offloaded to a Redis Cluster with read replicas.
3. Database writes (usage logs) are decoupled via in-process queues and batch commits, minimizing database IOPS.
4. Caching at the edge eliminates up to 40% of upstream network round-trips.

