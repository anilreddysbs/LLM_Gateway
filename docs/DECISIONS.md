# Architectural Decision Records (ADRs)

This document records the architectural and engineering decisions made during the design and construction of the LLM Gateway.

---

## ADR 001: Sliding Window Counter via Atomic Redis Lua vs Token Bucket

### Context
Upstream LLM providers (e.g. OpenAI, Google Gemini, Anthropic) enforce strict rate limits calculated per rolling 60-second window (RPM) and token volume (TPM). Exceeding these thresholds causes cascading upstream 429 failures.
Traditional Fixed-Window algorithms suffer from boundary burst vulnerabilities: a client can fire 100% of their quota at second 59 and another 100% at second 60, delivering a 2x traffic surge across the minute boundary.

### Decision
We implemented a **Sliding Window Log** algorithm backed by Redis Sorted Sets (`ZSET`) and executed via an atomic Lua script (`SLIDING_WINDOW_LUA`).
1. Every incoming request records a microsecond timestamp score into the tenant's `ZSET`.
2. Old timestamps outside the `(now - 60s)` window are evicted atomically via `ZREMRANGEBYSCORE`.
3. `ZCARD` computes the exact number of requests in the true rolling 60-second window.
4. If the count meets or exceeds the tenant's limit, the Lua script inspects the oldest timestamp via `ZRANGE ... WITHSCORES` and computes the exact integer seconds until quota frees up, returning an accurate `Retry-After` header.

### Consequences
- **Pros:** 100% mathematical prevention of boundary burst attacks. Atomic single-round-trip execution in Redis avoids race conditions across distributed gateway replicas.
- **Cons:** Higher storage overhead than fixed-window counters; however, `PEXPIRE` automatically garbage-collects inactive tenant keys, keeping memory footprint tightly bounded.

---

## ADR 002: Exact-Match Canonical SHA-256 Hashing vs Semantic Vector Cache

### Context
LLMs frequently receive identical queries (e.g., system prompts, deterministic data extraction, repetitive classification tasks). Calling upstream APIs repeatedly wastes money and increases latency.

### Decision
We implemented an **Exact-Match Redis Cache** using a deterministic SHA-256 hash of normalized request parameters (model, messages, tools, temperature, top_p, etc.).
We explicitly rejected embedding-based Semantic Caching for the primary gateway data plane:
1. Semantic caching introduces false positives where subtle prompt nuances (e.g., "Do NOT summarize" vs "Summarize") produce catastrophic errors if cosine similarity exceeds a threshold.
2. Generating vector embeddings for every request adds 50–150ms of upstream embedding latency, defeating the purpose of sub-5ms caching.
3. Exact-match caching guarantees zero semantic drift and executes in < 2ms directly in Redis.

### Consequences
- Requests with `temperature <= 0.3` are cache-eligible.
- Requests with `Cache-Control: no-cache` or `no-store` bypass or skip caching.
- Multi-tenant cache isolation (`isolate_cache=True`) partitions cache keys per API key to prevent cross-tenant data leakage.

---

## ADR 003: In-Process Asynchronous `asyncio.Queue` vs Distributed Workers (Celery / Kafka)

### Context
Audit logging, token counting, and cost tracking must occur for every request without increasing request latency on the critical path.

### Decision
We selected an in-process, non-blocking `asyncio.Queue` coupled with batched asynchronous persistence to PostgreSQL (`UsageLogQueue`):
1. Calling `UsageLogQueue.enqueue(...)` takes < 0.05ms (a memory pointer push).
2. A background worker task collects entries up to `USAGE_LOG_BATCH_SIZE` (default 50) or every `USAGE_LOG_FLUSH_INTERVAL_SECONDS` (default 2s) and commits them in a single batch database transaction.
3. Queue saturation triggers fail-safe dropping of audit records (`DROPPED_LOGS_TOTAL`) rather than blocking or failing incoming user completions.
4. Celery, RabbitMQ, and Kafka were deliberately rejected for this deployment tier to eliminate heavy operational dependencies and serialization overhead while preserving horizontal scalability via gateway instances.

### Consequences
- **Pros:** Zero external message broker dependency; zero serialization overhead; sub-millisecond logging latency.
- **Cons:** Process crashes could drop up to 50 uncommitted log entries. On graceful shutdown, the `lifespan` handler flushes all queued items completely.

---

## ADR 004: Redis Distributed Circuit Breaker vs Local In-Memory Circuit Breakers

### Context
When upstream providers degrade or experience outages, continuing to send traffic causes thread starvation, connection pool exhaustion, and prolonged client timeouts.

### Decision
We designed a **Distributed Circuit Breaker** (`CLOSED`, `OPEN`, `HALF_OPEN`) storing failure counts and cooldown timestamps in Redis:
1. If a model encounters 5 consecutive 5xx/timeout errors within the window, the circuit trips to `OPEN` for 30 seconds.
2. In-flight requests immediately fast-fail or skip to the next target in the fallback chain without attempting network I/O against the broken upstream.
3. Once the cooldown timer expires, the breaker transitions to `HALF_OPEN`, allowing a single probe request to test upstream health.
4. Using Redis ensures that circuit breaker state is synchronized across all gateway workers and cluster nodes immediately.

### Consequences
- Protects upstream providers from storming during recovery.
- Immediate failover minimizes user-visible latency spikes during upstream incidents.

---

## ADR 005: Fail-Open vs Fail-Closed Rate Limiter Availability Tradeoff

### Context
If the Redis cluster is unreachable or experiences transient network partition, rate-limiting and cache checks may fail.

### Decision
We provided a configurable setting: `RATE_LIMIT_FAIL_OPEN` (default: `True`).
- **Fail-Open (Default):** If Redis is down, log an alert and allow the request through to the upstream model. Prioritizes business availability over strict rate compliance.
- **Fail-Closed:** Returns HTTP 503 `service_unavailable` immediately. Ideal for strictly metered multi-tenant billing environments where over-quota usage cannot be tolerated.

---

## ADR 006: Server-Sent Events (SSE) Fallback-Before-First-Chunk Invariant

### Context
Streaming completions (`stream: true`) send chunks progressively over HTTP `text/event-stream`. If the upstream provider fails midway through the generation, how should the gateway respond?

### Decision
We established a strict architectural invariant:
1. **Fallback Allowed Before First Chunk:** If the primary provider fails *before* emitting any tokens, the gateway catches the exception, closes the primary stream, and seamlessly opens a stream to the secondary provider in the alias chain. The client receives a seamless HTTP 200 stream.
2. **Fallback Forbidden After First Chunk:** Once the client has received HTTP 200 and the initial delta chunk, HTTP headers cannot be altered and the output stream has begun. Trying to switch providers mid-stream would result in corrupted responses (style mismatch, repetitive content).
3. Therefore, an error after the first chunk MUST emit an in-stream SSE error event (`stream_interrupted`) and cleanly terminate with `[DONE]`.
