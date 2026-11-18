"""Master API router organizing versioned and operations endpoints."""

from fastapi import APIRouter

from llm_gateway.api.ops.health import router as health_router
from llm_gateway.api.v1.chat import router as chat_router
from llm_gateway.api.v1.models import router as models_router

api_router = APIRouter()

# Core service ops
api_router.include_router(health_router)

# V1 Public API
api_router.include_router(chat_router)
api_router.include_router(models_router)
