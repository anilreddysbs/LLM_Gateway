# LLM Gateway


> **Portfolio:** [anilreddysbs.vercel.app](https://anilreddysbs.vercel.app) · [GitHub profile](https://github.com/anilreddysbs)

[![Python](https://img.shields.io/badge/Python-3.12%20%7C%203.13-blue.svg)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115+-009688.svg)](https://fastapi.tiangolo.com)
[![PostgreSQL](https://img.shields.io/badge/PostgreSQL-16+-336791.svg)](https://www.postgresql.org/)
[![Redis](https://img.shields.io/badge/Redis-7.2+-DC382D.svg)](https://redis.io/)
[![Docker](https://img.shields.io/badge/Docker-Compose-2496ED.svg)](https://www.docker.com/)
[![License](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

A production-grade, high-performance, unified LLM API Gateway and Reverse Proxy built in Python with **FastAPI**, **Redis**, and **PostgreSQL**.

The gateway provides a single OpenAI-compatible interface to multiple upstream AI providers (Google Gemini, OpenAI, and compatible models) with enterprise-grade resilience: **sliding window rate limiting**, **exact-match distributed caching**, **automatic fallback routing**, **3-state circuit breaking**, **Server-Sent Events (SSE) streaming**, **real-time cost analytics**, and **Prometheus metrics**.

---

## Architecture Overview

```mermaid
flowchart TD
    Client(["Client Applications / AI SDKs"]) -->|"POST /v1/chat/completions"| GW["FastAPI LLM Gateway"]

    subgraph Security & Policy ["1. Policy & Admission Plane"]
        GW --> MW["RequestContextMiddleware<br/>(X-Request-ID ContextVar)"]
        MW --> Auth["API Key Verifier<br/>(HMAC SHA-256 + 60s Redis Cache)"]
        Auth --> Budget["Monthly USD Budget Guard<br/>(Cached Spend vs Limit)"]
        Budget --> RL["Sliding Window Rate Limiter<br/>(Atomic Redis Lua Script)"]
    end

    subgraph Data Plane ["2. Caching & Resilience Plane"]
        RL --> Router["Request Router<br/>(Alias to Fallback Chain)"]
        Router --> Cache{"Exact-Match Cache?<br/>(Canonical SHA-256)"}
        Cache -->|"Cache HIT<br/>(0 USD, &lt;2ms)"| ReturnCached["Return Cached Completion"]
        Cache -->|"Cache MISS"| Fallback["Fallback Executor"]
        
        subgraph Upstream Resilience
            Fallback --> CB["Redis Circuit Breaker<br/>(CLOSED / OPEN / HALF_OPEN)"]
            CB --> Retry["Exponential Backoff<br/>+ AWS Full Jitter"]
            Retry --> Primary["Primary Provider<br/>(e.g., Google Gemini)"]
            Retry -.->|"Failover on 5xx/Timeout"| Secondary["Secondary Fallback<br/>(e.g., OpenAI gpt-4o-mini)"]
        end
    end

    subgraph Observability ["3. Observability & Telemetry"]
        Fallback --> Queue["UsageLogQueue<br/>(In-Process asyncio.Queue)"]
        Queue -->|"Batched Async Writes"| PG[("PostgreSQL 16<br/>usage_logs")]
        Fallback --> Prom["Prometheus Metrics<br/>(/metrics)"]
        Fallback --> Logs["Structured JSON Logger<br/>(req_id, provider, latency)"]
    end

    Primary --> Response["Unified OpenAI Response + Headers"]
    Secondary --> Response
    ReturnCached --> Response
    Response --> Client
```

---

## Key Features

- **OpenAI-Compatible Drop-In Replacement:** Full support for `POST /v1/chat/completions` (both non-streaming and Server-Sent Events streaming) and `GET /v1/models`.
- **Sliding Window Rate Limiting:** Enforces strict requests-per-minute (RPM) quotas using an atomic Redis Lua script over a rolling 60-second window, completely preventing 2x boundary burst attacks. Returns accurate `Retry-After` headers.
- **Exact-Match Response Caching:** Computes deterministic SHA-256 hashes of canonicalized request bodies. Serves cached responses in < 2ms with zero USD cost and optional multi-tenant cache isolation.
- **Distributed Stampede Protection:** Uses Redis `SET ... NX EX` distributed locks to prevent thundering herds when hot cache keys expire.
- **Multi-Provider Fallback Routing:** Declarative routing aliases (`default`, `fast`, `quality`) that automatically failover across multiple providers on 5xx errors or network timeouts.
- **Distributed Circuit Breaker:** 3-state breaker (`CLOSED`, `OPEN`, `HALF_OPEN`) synchronized across cluster nodes via Redis to fast-fail traffic and protect recovering upstream models.
- **Streaming Fallback-Before-First-Chunk Invariant:** Allows failover before the first token delta is yielded; emits in-stream `stream_interrupted` SSE events if errors occur mid-stream to prevent corrupted mixed outputs.
- **High-Precision Financial Accounting:** Tracks token usage and calculates USD costs with Python `Decimal` arithmetic.
- **Asynchronous In-Process Logging:** Non-blocking `asyncio.Queue` buffers telemetry and flushes batched logs to PostgreSQL without adding latency to inference calls.
- **Production Observability:** Prometheus scrape endpoint (`/metrics`), standard health/readiness probes (`/health`, `/ready`), and administrative audit endpoints (`/admin/keys`, `/admin/usage`, `/admin/stats`).

---

## Performance Benchmarks

Measured using the production load testing harness (`scripts/load_test.py`) with concurrent ASGI workers:

| Scenario | Total Reqs | Success Rate | 429 Rejections | Throughput (RPS) | p50 Latency | p95 Latency | Cache Hit Rate |
|:---|:---:|:---:|:---:|:---:|:---:|:---:|:---:|
| **Baseline (Cache Misses)** | 100 | 100% | 0 | **63.2 RPS** | 133.8 ms | 316.8 ms | 0.0% |
| **Exact-Match Cache Hits** | 200 | 100% | 0 | **60.0 RPS** | < 5 ms* | 407.1 ms | **100.0%** |
| **Rate Limit Saturation (429)** | 50 | 20% | 40 | **33.9 RPS** | 338.5 ms | 1038.9 ms | 0.0% |
| **Fallback Failover Under Outage** | 50 | 100% | 0 | **29.0 RPS** | 30.7 ms | 1020.6 ms | 0.0% |

*\*Cache hits served directly from Redis without upstream LLM network round-trips.* Full analysis in [docs/benchmarks.md](docs/benchmarks.md).

---

## Quickstart

### Option 1: Run with Docker Compose (Recommended)

1. Clone repository and copy environment configuration:
   ```bash
   git clone https://github.com/anilreddysbs/LLM_Gateway.git
   cd LLM_Gateway
   cp .env.example .env
   ```

2. Add your provider API keys to `.env`:
   ```dotenv
   GEMINI_API_KEY=your_gemini_api_key_here
   OPENAI_API_KEY=your_openai_api_key_here
   ADMIN_API_KEY=admin-super-secret-key
   ```

3. Build and start services:
   ```bash
   docker compose up -d --build
   ```

4. Verify service health:
   ```bash
   curl http://localhost:8000/health
   curl http://localhost:8000/ready
   ```

---

### Option 2: Local Development Setup

1. Create and activate a Python 3.12+ virtual environment:
   ```bash
   python -m venv .venv
   source .venv/bin/activate  # On Windows: .\.venv\Scripts\Activate.ps1
   ```

2. Install dependencies:
   ```bash
   pip install -e ".[dev]"
   ```

3. Ensure PostgreSQL and Redis are running locally, then apply database migrations:
   ```bash
   alembic upgrade head
   ```

4. Launch the gateway server:
   ```bash
   uvicorn llm_gateway.main:create_app --factory --host 0.0.0.0 --port 8000 --reload
   ```

---

## API Usage Examples

### 1. Provision a Client API Key
```bash
curl -X POST "http://localhost:8000/admin/keys" \
  -H "X-Admin-Key: admin-super-secret-key" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Production Service Key",
    "requests_per_minute": 60,
    "monthly_budget_usd": 100.0,
    "isolate_cache": false
  }'
```
*Returns the plaintext `gw-live-...` key. Store this securely.*

---

### 2. Standard Chat Completion
```bash
curl -X POST "http://localhost:8000/v1/chat/completions" \
  -H "Authorization: Bearer gw-live-YOUR_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "default",
    "messages": [
      {"role": "system", "content": "You are a concise engineering assistant."},
      {"role": "user", "content": "What is the primary benefit of sliding window rate limiting?"}
    ],
    "temperature": 0.2
  }'
```

---

### 3. Server-Sent Events (SSE) Streaming
```bash
curl -N -X POST "http://localhost:8000/v1/chat/completions" \
  -H "Authorization: Bearer gw-live-YOUR_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "fast",
    "messages": [{"role": "user", "content": "List 3 advantages of asynchronous I/O."}],
    "stream": true
  }'
```

---

### 4. Admin Usage & Cost Analytics
```bash
# Query filtered usage audit logs
curl "http://localhost:8000/admin/usage?limit=10" \
  -H "X-Admin-Key: admin-super-secret-key"

# Query aggregate stats (hit rate, error rate, p50/p95/p99 latencies, cost breakdown)
curl "http://localhost:8000/admin/stats" \
  -H "X-Admin-Key: admin-super-secret-key"
```

---

### 5. Prometheus Metrics Scrape
```bash
curl http://localhost:8000/metrics
```

---

## Configuration Reference

### Environment Variables (`.env`)

| Variable | Default | Description |
|:---|:---:|:---|
| `ENVIRONMENT` | `production` | Environment mode (`development`, `test`, `production`). |
| `DEBUG` | `false` | Enable verbose debug logging. |
| `DATABASE_URL` | - | PostgreSQL connection URL (`postgresql+asyncpg://user:pass@host/db`). |
| `REDIS_URL` | - | Redis connection URL (`redis://localhost:6379/0`). |
| `ADMIN_API_KEY` | - | Secret bearer key required for `/admin/*` endpoints. |
| `GEMINI_API_KEY` | - | Google Generative Language API key. |
| `OPENAI_API_KEY` | - | OpenAI API key. |
| `RATE_LIMIT_FAIL_OPEN` | `true` | If True, permits requests if Redis is temporarily unreachable. |
| `CACHE_TTL_SECONDS` | `86400` | Default response cache lifetime (24 hours). |
| `CIRCUIT_BREAKER_FAILURE_THRESHOLD` | `5` | Consecutive failures before tripping to `OPEN`. |
| `CIRCUIT_BREAKER_COOLDOWN_SECONDS` | `30` | Seconds to hold circuit `OPEN` before probing. |

---

## Project Structure

```
├── config/
│   └── models.yaml              # Declarative model definitions, pricing & aliases
├── docs/
│   ├── DECISIONS.md             # Architectural Decision Records (ADRs)
│   ├── LEARNING.md              # Plain-language engineering walkthrough & 15 Q&As
│   ├── INTERVIEW_PREP.md        # 25 deep production engineering interview Q&As
│   └── benchmarks.md            # Real measured load test benchmark numbers
├── migrations/                  # Alembic database migrations
├── scripts/
│   ├── demo.sh                  # Executable feature demonstration script
│   └── load_test.py             # Concurrent load testing & benchmarking harness
├── src/llm_gateway/
│   ├── api/                     # FastAPI route controllers (v1, admin, ops)
│   ├── config/                  # Pydantic Settings and models.yaml loader
│   ├── core/                    # Rate limiting, caching, routing, resilience
│   ├── db/                      # SQLAlchemy models, repositories & session
│   ├── observability/           # Logging, Prometheus metrics, background queue
│   └── providers/               # Upstream adapters (Gemini, OpenAI, Mock)
├── tests/
│   ├── integration/             # End-to-end integration tests
│   └── unit/                    # Fast isolated unit tests
├── Dockerfile                   # Multi-stage container build
├── docker-compose.yml           # Complete containerized stack (App, Postgres, Redis)
└── pyproject.toml               # Poetry/Hatch build configuration
```

---

## Documentation Links
- [Architectural Decision Records (ADRs)](docs/DECISIONS.md)
- [Codebase Learning Guide & 15 Q&As](docs/LEARNING.md)
- [Production Engineering Interview Preparation (25 Q&As)](docs/INTERVIEW_PREP.md)
- [Measured Performance Benchmarks](docs/benchmarks.md)

---

## License
This project is licensed under the MIT License - see the LICENSE file for details.
