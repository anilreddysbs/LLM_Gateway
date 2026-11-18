"""FastAPI application entrypoint with lifespan, middleware, and route mounting."""

import time
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from llm_gateway.api.router import api_router
from llm_gateway.config.settings import get_settings
from llm_gateway.db.session import close_db, init_db
from llm_gateway.observability.logging import logger, request_id_ctx, setup_logging
from llm_gateway.schemas.errors import GatewayException


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Middleware attaching X-Request-ID and measuring overall request duration."""

    async def dispatch(self, request: Request, call_next):
        # Preserve client-provided X-Request-ID or generate new UUID4
        request_id = request.headers.get("X-Request-ID") or f"req_{uuid.uuid4().hex[:16]}"
        token = request_id_ctx.set(request_id)

        start_time = time.perf_counter()
        try:
            response: Response = await call_next(request)
            duration_ms = (time.perf_counter() - start_time) * 1000

            response.headers["X-Request-ID"] = request_id
            logger.info(
                f"{request.method} {request.url.path} completed "
                f"with status {response.status_code} in {duration_ms:.2f}ms",
                extra={
                    "method": request.method,
                    "path": request.url.path,
                    "status_code": response.status_code,
                    "duration_ms": round(duration_ms, 2),
                },
            )
            return response
        finally:
            request_id_ctx.reset(token)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan managing database connections and graceful startup/shutdown."""
    settings = get_settings()
    setup_logging(level="DEBUG" if settings.DEBUG else "INFO")
    logger.info("Starting LLM Gateway service", extra={"environment": settings.ENVIRONMENT})

    # Ensure database schema is initialized
    try:
        await init_db()
        logger.info("Database initialized successfully")
    except Exception as exc:
        logger.error(f"Failed to initialize database during startup: {exc}")

    yield

    logger.info("Shutting down LLM Gateway service")
    await close_db()


def create_app() -> FastAPI:
    """Application factory creating configured FastAPI app instance."""
    app = FastAPI(
        title="LLM Gateway",
        description="Unified OpenAI-compatible gateway with resilience and caching.",
        version="0.1.0",
        lifespan=lifespan,
    )

    # CORS middleware
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Request ID and tracing middleware
    app.add_middleware(RequestContextMiddleware)

    # Standard OpenAI error handlers
    @app.exception_handler(GatewayException)
    async def gateway_exception_handler(_: Request, exc: GatewayException):
        return JSONResponse(
            status_code=exc.status_code,
            content=exc.detail,
            headers=exc.headers,
        )

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(_: Request, exc: RequestValidationError):
        errors = exc.errors()
        first_err = errors[0] if errors else {}
        param = str(first_err.get("loc", [""])[-1]) if first_err.get("loc") else None
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "message": first_err.get("msg", "Validation error"),
                    "type": "invalid_request_error",
                    "code": "validation_error",
                    "param": param,
                }
            },
        )

    # Mount master API router
    app.include_router(api_router)

    return app


app = create_app()
