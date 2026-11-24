#!/usr/bin/env bash
# ==============================================================================
# LLM Gateway Live Feature Demo Script
# Demonstrates: Health, Cache Miss, Cache Hit, SSE Streaming, Fallback, Rate Limiting, Metrics, Admin
# ==============================================================================

set -e

GATEWAY_URL=${GATEWAY_URL:-"http://localhost:8000"}
ADMIN_API_KEY=${ADMIN_API_KEY:-"admin-super-secret-key"}

echo "======================================================================"
echo "LLM GATEWAY LIVE CLI DEMO"
echo "Target Host: $GATEWAY_URL"
echo "======================================================================"

# 1. Health & Readiness Probe
echo -e "\n[1] Checking Gateway Liveness & Readiness..."
curl -s -i "$GATEWAY_URL/health" | head -n 5
curl -s "$GATEWAY_URL/ready"
echo ""

# 2. Provision a Client API Key
echo -e "\n[2] Provisioning a New Client API Key via /admin/keys..."
KEY_RESPONSE=$(curl -s -X POST "$GATEWAY_URL/admin/keys" \
  -H "X-Admin-Key: $ADMIN_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Demo Client Key",
    "requests_per_minute": 5,
    "monthly_budget_usd": 50.0,
    "isolate_cache": false
  }')

echo "Response: $KEY_RESPONSE"
CLIENT_KEY=$(echo "$KEY_RESPONSE" | grep -o '"raw_key":"[^"]*' | cut -d'"' -f4)

if [ -z "$CLIENT_KEY" ]; then
  echo "Error provisioning API key. Using fallback demo key."
  CLIENT_KEY="gw-live-demo-key"
fi

echo "Created Key: $CLIENT_KEY"

# 3. Non-Streaming Chat Completion (Cache Miss)
echo -e "\n[3] Sending First Chat Completion (Expecting Cache MISS)..."
curl -s -i -X POST "$GATEWAY_URL/v1/chat/completions" \
  -H "Authorization: Bearer $CLIENT_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "default",
    "messages": [{"role": "user", "content": "Explain quantum computing in one sentence."}],
    "temperature": 0.2
  }' | grep -E "(HTTP/|X-Gateway-|X-RateLimit-)"

# 4. Repeated Request (Cache Hit)
echo -e "\n[4] Sending Identical Prompt Again (Expecting Cache HIT - 0 USD cost, <5ms latency)..."
curl -s -i -X POST "$GATEWAY_URL/v1/chat/completions" \
  -H "Authorization: Bearer $CLIENT_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "default",
    "messages": [{"role": "user", "content": "Explain quantum computing in one sentence."}],
    "temperature": 0.2
  }' | grep -E "(HTTP/|X-Gateway-|X-RateLimit-)"

# 5. Server-Sent Events (SSE) Streaming
echo -e "\n[5] Executing SSE Streaming Completion (/v1/chat/completions with stream=true)..."
curl -N -s -X POST "$GATEWAY_URL/v1/chat/completions" \
  -H "Authorization: Bearer $CLIENT_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "fast",
    "messages": [{"role": "user", "content": "Count from 1 to 5"}],
    "stream": true
  }' | head -n 15

# 6. Rate Limit Saturation
echo -e "\n[6] Rapidly Firing Requests to Exceed 5 RPM Limit (Expecting 429)..."
for i in {1..7}; do
  echo -n "Request $i: "
  curl -s -o /dev/null -w "%{http_code}\n" -X POST "$GATEWAY_URL/v1/chat/completions" \
    -H "Authorization: Bearer $CLIENT_KEY" \
    -H "Content-Type: application/json" \
    -d '{
      "model": "fast",
      "messages": [{"role": "user", "content": "Ping"}]
    }'
done

# 7. Scrape Prometheus Telemetry Metrics
echo -e "\n[7] Scraping Prometheus Metrics (/metrics)..."
curl -s "$GATEWAY_URL/metrics" | grep -E "^llm_gateway_" | head -n 12

# 8. Query Admin Stats Analytics
echo -e "\n[8] Querying Aggregated Admin Operational Stats (/admin/stats)..."
curl -s "$GATEWAY_URL/admin/stats" \
  -H "X-Admin-Key: $ADMIN_API_KEY"

echo -e "\n\n======================================================================"
echo "DEMO COMPLETED SUCCESSFULLY!"
echo "======================================================================"

