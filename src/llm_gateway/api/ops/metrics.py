"""Prometheus scrape endpoint exposing operational and business metrics."""

from fastapi import APIRouter, Response

from llm_gateway.observability.metrics import get_metrics_content

router = APIRouter(tags=["Operations"])


@router.get("/metrics")
async def metrics() -> Response:
    """Expose Prometheus telemetry metrics for scraping."""
    content, media_type = get_metrics_content()
    return Response(content=content, media_type=media_type)
