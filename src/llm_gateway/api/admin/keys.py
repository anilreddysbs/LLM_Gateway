"""Admin API key management endpoints."""

from fastapi import APIRouter, Depends, Query, status
from redis.asyncio import Redis

from llm_gateway.api.dependencies import get_api_key_repo, get_redis, verify_admin_key
from llm_gateway.core.auth import generate_api_key, invalidate_key_cache
from llm_gateway.db.repositories.api_key_repo import ApiKeyRepository
from llm_gateway.schemas.admin import (
    ApiKeyCreateRequest,
    ApiKeyCreateResponse,
    ApiKeyListResponse,
    ApiKeyResponse,
)
from llm_gateway.schemas.errors import GatewayException

router = APIRouter(
    prefix="/admin/keys", tags=["Admin Keys"], dependencies=[Depends(verify_admin_key)]
)


@router.post("", response_model=ApiKeyCreateResponse, status_code=status.HTTP_201_CREATED)
async def create_api_key(
    payload: ApiKeyCreateRequest,
    repo: ApiKeyRepository = Depends(get_api_key_repo),
) -> ApiKeyCreateResponse:
    """Issue a new client API key. The plaintext key is returned once in the response."""
    raw_key, key_hash, key_prefix = generate_api_key()

    db_key = await repo.create(
        name=payload.name,
        key_hash=key_hash,
        key_prefix=key_prefix,
        requests_per_minute=payload.requests_per_minute,
        monthly_budget_usd=payload.monthly_budget_usd,
        isolate_cache=payload.isolate_cache,
    )

    return ApiKeyCreateResponse(
        id=db_key.id,
        name=db_key.name,
        raw_key=raw_key,
        key_prefix=db_key.key_prefix,
        requests_per_minute=db_key.requests_per_minute,
        monthly_budget_usd=db_key.monthly_budget_usd,
        isolate_cache=db_key.isolate_cache,
        created_at=db_key.created_at,
    )


@router.get("", response_model=ApiKeyListResponse)
async def list_api_keys(
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    repo: ApiKeyRepository = Depends(get_api_key_repo),
) -> ApiKeyListResponse:
    """List issued client API keys (never returns plaintext secrets)."""
    keys, total = await repo.list_all(limit=limit, offset=offset)
    return ApiKeyListResponse(
        keys=[
            ApiKeyResponse(
                id=k.id,
                name=k.name,
                key_prefix=k.key_prefix,
                requests_per_minute=k.requests_per_minute,
                monthly_budget_usd=k.monthly_budget_usd,
                is_revoked=k.is_revoked,
                isolate_cache=k.isolate_cache,
                created_at=k.created_at,
                updated_at=k.updated_at,
            )
            for k in keys
        ],
        total=total,
    )


@router.delete("/{key_id}", status_code=status.HTTP_200_OK)
async def revoke_api_key(
    key_id: str,
    repo: ApiKeyRepository = Depends(get_api_key_repo),
    redis_client: Redis = Depends(get_redis),
) -> dict[str, str]:
    """Revoke an active API key immediately and purge its cache entry."""
    revoked = await repo.revoke(key_id)
    if not revoked:
        raise GatewayException(
            status_code=404,
            message=f"API key with ID '{key_id}' not found.",
            error_type="not_found_error",
        )

    # Invalidate Redis lookup cache
    await invalidate_key_cache(redis_client, revoked.key_hash)

    return {"status": "revoked", "id": key_id}
