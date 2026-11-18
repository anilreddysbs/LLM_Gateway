"""Database repository for querying usage logs and computing aggregate statistics."""

import math
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from llm_gateway.db.models import UsageLog


def calculate_percentile(sorted_data: list[float], percentile: float) -> float:
    """Calculate percentile value from a sorted list of numbers using linear interpolation."""
    if not sorted_data:
        return 0.0
    k = (len(sorted_data) - 1) * percentile
    f = math.floor(k)
    c = math.ceil(k)
    if f == c:
        return round(float(sorted_data[int(k)]), 2)
    d0 = float(sorted_data[int(f)]) * (c - k)
    d1 = float(sorted_data[int(c)]) * (k - f)
    return round(d0 + d1, 2)


class UsageLogRepository:
    """Provides filtered queries and analytical aggregations over the usage_logs table."""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def query_logs(
        self,
        request_id: str | None = None,
        api_key_id: str | None = None,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
        model: str | None = None,
        provider: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[UsageLog], int]:
        """Query usage logs with filtering and pagination."""
        filters = []
        if request_id:
            filters.append(UsageLog.request_id == request_id)
        if api_key_id:
            filters.append(UsageLog.api_key_id == api_key_id)
        if date_from:
            filters.append(UsageLog.created_at >= date_from)
        if date_to:
            filters.append(UsageLog.created_at <= date_to)
        if model:
            filters.append(UsageLog.model == model)
        if provider:
            filters.append(UsageLog.provider == provider)

        # Count total matches
        count_stmt = select(func.count(UsageLog.id))
        if filters:
            count_stmt = count_stmt.where(*filters)
        total = (await self.session.execute(count_stmt)).scalar_one()

        # Query page
        stmt = (
            select(UsageLog)
            .where(*filters)
            .order_by(UsageLog.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all()), total

    async def query_stats(
        self,
        api_key_id: str | None = None,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
    ) -> dict[str, Any]:
        """Compute aggregated performance metrics, cost breakdowns, and percentiles."""
        filters = []
        if api_key_id:
            filters.append(UsageLog.api_key_id == api_key_id)
        if date_from:
            filters.append(UsageLog.created_at >= date_from)
        if date_to:
            filters.append(UsageLog.created_at <= date_to)

        stmt = select(UsageLog).where(*filters)
        result = await self.session.execute(stmt)
        rows = list(result.scalars().all())

        total_requests = len(rows)
        if total_requests == 0:
            return {
                "total_requests": 0,
                "cache_hit_rate": 0.0,
                "error_rate": 0.0,
                "fallback_rate": 0.0,
                "p50_latency_ms": 0.0,
                "p95_latency_ms": 0.0,
                "p99_latency_ms": 0.0,
                "total_tokens": 0,
                "total_cost_usd": Decimal("0.000000"),
                "cost_by_model": {},
                "cost_by_provider": {},
                "requests_per_day": {},
            }

        cache_hits = 0
        errors = 0
        fallbacks = 0
        total_tokens = 0
        total_cost = Decimal("0.0")
        latencies: list[float] = []
        cost_by_model: dict[str, Decimal] = {}
        cost_by_provider: dict[str, Decimal] = {}
        requests_per_day: dict[str, int] = {}

        for row in rows:
            if row.cache_hit:
                cache_hits += 1
            if row.status_code >= 400:
                errors += 1
            if row.fallback_used:
                fallbacks += 1

            total_tokens += (row.input_tokens + row.output_tokens)
            total_cost += row.cost_usd
            latencies.append(float(row.latency_ms))

            # By model
            cost_by_model[row.model] = cost_by_model.get(row.model, Decimal("0.0")) + row.cost_usd

            # By provider
            cost_by_provider[row.provider] = cost_by_provider.get(row.provider, Decimal("0.0")) + row.cost_usd

            # By day
            day_str = row.created_at.strftime("%Y-%m-%d")
            requests_per_day[day_str] = requests_per_day.get(day_str, 0) + 1

        latencies.sort()
        p50 = calculate_percentile(latencies, 0.50)
        p95 = calculate_percentile(latencies, 0.95)
        p99 = calculate_percentile(latencies, 0.99)

        return {
            "total_requests": total_requests,
            "cache_hit_rate": round(cache_hits / total_requests, 4),
            "error_rate": round(errors / total_requests, 4),
            "fallback_rate": round(fallbacks / total_requests, 4),
            "p50_latency_ms": p50,
            "p95_latency_ms": p95,
            "p99_latency_ms": p99,
            "total_tokens": total_tokens,
            "total_cost_usd": total_cost.quantize(Decimal("0.000001")),
            "cost_by_model": {m: c.quantize(Decimal("0.000001")) for m, c in cost_by_model.items()},
            "cost_by_provider": {p: c.quantize(Decimal("0.000001")) for p, c in cost_by_provider.items()},
            "requests_per_day": requests_per_day,
        }
