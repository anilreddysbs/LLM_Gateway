"""Admin usage logs audit query endpoints."""

from datetime import datetime

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from llm_gateway.api.dependencies import verify_admin_key
from llm_gateway.db.repositories.usage_log_repo import UsageLogRepository
from llm_gateway.db.session import get_db_session
from llm_gateway.schemas.admin import UsageLogRow, UsageQueryResponse

router = APIRouter(prefix="/admin/usage", tags=["Admin Usage"], dependencies=[Depends(verify_admin_key)])


@router.get("", response_model=UsageQueryResponse)
async def query_usage_logs(
    request_id: str | None = Query(None, description="Filter by request ID"),
    api_key_id: str | None = Query(None, description="Filter by client API key ID"),
    date_from: datetime | None = Query(None, description="Start date (ISO-8601)"),
    date_to: datetime | None = Query(None, description="End date (ISO-8601)"),
    model: str | None = Query(None, description="Filter by actual model used"),
    provider: str | None = Query(None, description="Filter by upstream provider"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(get_db_session),
) -> UsageQueryResponse:
    """Query usage logs with multi-attribute filtering and pagination."""
    repo = UsageLogRepository(session)
    logs, total = await repo.query_logs(
        request_id=request_id,
        api_key_id=api_key_id,
        date_from=date_from,
        date_to=date_to,
        model=model,
        provider=provider,
        limit=limit,
        offset=offset,
    )

    return UsageQueryResponse(
        logs=[
            UsageLogRow(
                id=log.id,
                request_id=log.request_id,
                api_key_id=log.api_key_id,
                created_at=log.created_at,
                alias=log.alias,
                requested_model=log.requested_model,
                provider=log.provider,
                model=log.model,
                input_tokens=log.input_tokens,
                output_tokens=log.output_tokens,
                cost_usd=log.cost_usd,
                latency_ms=float(log.latency_ms),
                cache_hit=log.cache_hit,
                fallback_used=log.fallback_used,
                attempts=log.attempts,
                status_code=log.status_code,
                error_type=log.error_type,
                streaming=log.streaming,
                estimated_tokens=log.estimated_tokens,
            )
            for log in logs
        ],
        total=total,
    )
