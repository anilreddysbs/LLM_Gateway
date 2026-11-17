"""Health and readiness probe endpoints."""

import logging
from typing import Any

from fastapi import APIRouter, Response, status
from redis.asyncio import Redis
from sqlalchemy import text

from llm_gateway.config.settings import get_settings
from llm_gateway.db.session import get_engine

router = APIRouter(tags=["Operations"])
logger = logging.getLogger("llm_gateway.health")


@router.get("/health", status_code=status.HTTP_200_OK)
async def health_check() -> dict[str, str]:
    """Liveness probe indicating the service process is up and running."""
    return {"status": "ok", "version": "0.1.0"}


@router.get("/ready")
async def readiness_check(response: Response) -> dict[str, Any]:
    """Readiness probe verifying PostgreSQL and Redis connectivity."""
    settings = get_settings()
    results: dict[str, str] = {
        "database": "unknown",
        "redis": "unknown",
    }
    is_ready = True

    # 1. Check Database
    try:
        engine = get_engine()
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        results["database"] = "connected"
    except Exception as exc:
        logger.warning(f"Database readiness check failed: {exc}")
        results["database"] = f"unhealthy: {exc.__class__.__name__}"
        is_ready = False

    # 2. Check Redis
    try:
        redis_client = Redis.from_url(settings.REDIS_URL, decode_responses=True)
        pong = await redis_client.ping()
        await redis_client.aclose()
        if pong:
            results["redis"] = "connected"
        else:
            results["redis"] = "unresponsive"
            is_ready = False
    except Exception as exc:
        logger.warning(f"Redis readiness check failed: {exc}")
        results["redis"] = f"unhealthy: {exc.__class__.__name__}"
        is_ready = False

    if not is_ready:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {"status": "not_ready", "checks": results}

    return {"status": "ready", "checks": results}
