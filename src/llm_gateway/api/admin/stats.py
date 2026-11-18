"""Admin aggregated metrics and cost statistics endpoints."""

from datetime import datetime

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from llm_gateway.api.dependencies import verify_admin_key
from llm_gateway.db.repositories.usage_log_repo import UsageLogRepository
from llm_gateway.db.session import get_db_session
from llm_gateway.schemas.admin import AdminStatsResponse

router = APIRouter(prefix="/admin/stats", tags=["Admin Stats"], dependencies=[Depends(verify_admin_key)])


@router.get("", response_model=AdminStatsResponse)
async def get_aggregated_stats(
    api_key_id: str | None = Query(None, description="Filter stats by API key"),
    date_from: datetime | None = Query(None, description="Start date (ISO-8601)"),
    date_to: datetime | None = Query(None, description="End date (ISO-8601)"),
    session: AsyncSession = Depends(get_db_session),
) -> AdminStatsResponse:
    """Retrieve aggregated gateway statistics: hit rates, percentiles, and cost breakdowns."""
    repo = UsageLogRepository(session)
    stats_data = await repo.query_stats(
        api_key_id=api_key_id,
        date_from=date_from,
        date_to=date_to,
    )
    return AdminStatsResponse(**stats_data)
