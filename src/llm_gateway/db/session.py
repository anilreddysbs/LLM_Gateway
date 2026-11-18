"""Database connection engine, session factory, and dependency injection."""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

from llm_gateway.config.settings import get_settings
from llm_gateway.db.models import Base

_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def get_engine() -> AsyncEngine:
    """Initialize or return the global singleton SQLAlchemy async engine."""
    global _engine
    if _engine is None:
        settings = get_settings()
        url = settings.DATABASE_URL

        # Special handling for sqlite in test mode vs postgres in production
        if "sqlite" in url:
            _engine = create_async_engine(
                url,
                echo=settings.DEBUG,
                connect_args={"check_same_thread": False},
                poolclass=StaticPool,
            )
        else:
            _engine = create_async_engine(
                url,
                echo=settings.DEBUG,
                pool_size=20,
                max_overflow=10,
                pool_pre_ping=True,
            )
    return _engine


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    """Initialize or return the global singleton session factory."""
    global _sessionmaker
    if _sessionmaker is None:
        engine = get_engine()
        _sessionmaker = async_sessionmaker(
            bind=engine,
            class_=AsyncSession,
            expire_on_commit=False,
            autoflush=False,
        )
    return _sessionmaker


async def get_db_session() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency yielding an async database session."""
    session_factory = get_sessionmaker()
    async with session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


@asynccontextmanager
async def get_db_context() -> AsyncGenerator[AsyncSession, None]:
    """Context manager for standalone background tasks requiring a DB session."""
    session_factory = get_sessionmaker()
    async with session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def init_db() -> None:
    """Create all database tables if they do not exist (useful for testing and initial run)."""
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def close_db() -> None:
    """Dispose the database engine and connection pool during application shutdown."""
    global _engine, _sessionmaker
    if _engine is not None:
        await _engine.dispose()
        _engine = None
        _sessionmaker = None
