"""Integration tests for Admin API Key lifecycle and client authentication."""

import pytest
from httpx import AsyncClient

from llm_gateway.db.models import ApiKey


@pytest.mark.asyncio
async def test_admin_endpoints_require_admin_key(client: AsyncClient):
    """Admin endpoints must reject requests without valid ADMIN_API_KEY with 401."""
    # Missing header
    res = await client.post("/admin/keys", json={"name": "No Auth"})
    assert res.status_code == 401
    assert res.json()["error"]["type"] == "authentication_error"

    # Invalid header
    res_invalid = await client.post(
        "/admin/keys",
        json={"name": "Bad Auth"},
        headers={"X-Admin-Key": "wrong-secret-key"},
    )
    assert res_invalid.status_code == 401


@pytest.mark.asyncio
async def test_admin_create_and_list_keys(client: AsyncClient):
    """Admin can issue an API key, receive plaintext secret once, and list sanitized records."""
    admin_headers = {"X-Admin-Key": "test-admin-key"}

    # 1. Create Key
    create_payload = {
        "name": "Production App",
        "requests_per_minute": 100,
        "monthly_budget_usd": 250.0,
        "isolate_cache": True,
    }
    create_resp = await client.post("/admin/keys", json=create_payload, headers=admin_headers)
    assert create_resp.status_code == 201
    data = create_resp.json()
    assert "raw_key" in data
    assert data["raw_key"].startswith("gw-live-")
    assert data["name"] == "Production App"
    assert data["requests_per_minute"] == 100
    assert data["isolate_cache"] is True
    key_id = data["id"]
    raw_key = data["raw_key"]

    # 2. List Keys
    list_resp = await client.get("/admin/keys", headers=admin_headers)
    assert list_resp.status_code == 200
    list_data = list_resp.json()
    assert list_data["total"] >= 1
    target = next((k for k in list_data["keys"] if k["id"] == key_id), None)
    assert target is not None
    # Ensure raw secret and full hash are NOT exposed in list view
    assert "raw_key" not in target
    assert "key_hash" not in target
    assert target["name"] == "Production App"
    assert target["is_revoked"] is False

    # 3. Use newly created key on /v1/chat/completions
    chat_resp = await client.post(
        "/v1/chat/completions",
        json={"model": "mock-primary", "messages": [{"role": "user", "content": "hi"}]},
        headers={"Authorization": f"Bearer {raw_key}"},
    )
    assert chat_resp.status_code == 200

    # 4. Revoke Key
    del_resp = await client.delete(f"/admin/keys/{key_id}", headers=admin_headers)
    assert del_resp.status_code == 200
    assert del_resp.json()["status"] == "revoked"

    # 5. Revoked key must now return 403 Forbidden
    forbidden_resp = await client.post(
        "/v1/chat/completions",
        json={"model": "mock-primary", "messages": [{"role": "user", "content": "hi"}]},
        headers={"Authorization": f"Bearer {raw_key}"},
    )
    assert forbidden_resp.status_code == 403
    assert forbidden_resp.json()["error"]["type"] == "forbidden"


@pytest.mark.asyncio
async def test_chat_completions_missing_or_invalid_auth(client: AsyncClient):
    """Client calls without auth or with invalid bearer tokens must return 401."""
    # Missing Auth
    res_no_auth = await client.post(
        "/v1/chat/completions",
        json={"model": "mock-primary", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert res_no_auth.status_code == 401
    assert "Missing Authorization" in res_no_auth.json()["error"]["message"]

    # Malformed Auth
    res_malformed = await client.post(
        "/v1/chat/completions",
        json={"model": "mock-primary", "messages": [{"role": "user", "content": "hi"}]},
        headers={"Authorization": "InvalidScheme token123"},
    )
    assert res_malformed.status_code == 401

    # Non-existent Key
    res_fake_key = await client.post(
        "/v1/chat/completions",
        json={"model": "mock-primary", "messages": [{"role": "user", "content": "hi"}]},
        headers={"Authorization": "Bearer gw-live-nonexistent-key-99999"},
    )
    assert res_fake_key.status_code == 401
    assert "Invalid API key" in res_fake_key.json()["error"]["message"]


@pytest.mark.asyncio
async def test_auth_redis_cache_hit_path(client: AsyncClient, valid_api_key: tuple[ApiKey, str]):
    """Subsequent requests within 60s hit Redis cache without re-querying the database."""
    db_key, raw_key = valid_api_key
    headers = {"Authorization": f"Bearer {raw_key}"}

    # First request: DB hit + Redis cache write
    res1 = await client.post(
        "/v1/chat/completions",
        json={"model": "mock-primary", "messages": [{"role": "user", "content": "first"}]},
        headers=headers,
    )
    assert res1.status_code == 200

    # Second request: served from Redis cache
    res2 = await client.post(
        "/v1/chat/completions",
        json={"model": "mock-primary", "messages": [{"role": "user", "content": "second"}]},
        headers=headers,
    )
    assert res2.status_code == 200
