"""Unit and integration tests for /health and /ready endpoints."""

import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_health_endpoint(client: AsyncClient):
    """GET /health should return 200 with status ok and request ID header."""
    response = await client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert "version" in data
    assert "X-Request-ID" in response.headers


@pytest.mark.asyncio
async def test_health_with_custom_request_id(client: AsyncClient):
    """Client-provided X-Request-ID should be preserved in response headers."""
    custom_id = "test-req-custom-12345"
    response = await client.get("/health", headers={"X-Request-ID": custom_id})
    assert response.status_code == 200
    assert response.headers["X-Request-ID"] == custom_id
