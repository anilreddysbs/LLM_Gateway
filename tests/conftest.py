"""Pytest configuration and shared async test fixtures."""

import os
from collections.abc import AsyncGenerator

import fakeredis.aioredis
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

# Force test environment variables before imports
os.environ["ENVIRONMENT"] = "test"
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///:memory:"
os.environ["REDIS_URL"] = "redis://localhost:6379/0"
os.environ["ADMIN_API_KEY"] = "test-admin-key"

from llm_gateway.api.dependencies import get_redis
from llm_gateway.config.models_config import ModelsConfig, load_models_config
from llm_gateway.config.settings import Settings, get_settings
from llm_gateway.core.auth import generate_api_key
from llm_gateway.db.models import ApiKey, Base
from llm_gateway.db.repositories.api_key_repo import ApiKeyRepository
from llm_gateway.db.session import get_db_session
from llm_gateway.main import create_app


@pytest.fixture(scope="session")
def test_settings() -> Settings:
    """Return test settings singleton."""
    return get_settings()


@pytest.fixture(scope="session")
def models_config() -> ModelsConfig:
    """Load models configuration for tests."""
    return load_models_config("config/models.yaml")


@pytest_asyncio.fixture
async def test_db_session() -> AsyncGenerator[AsyncSession, None]:
    """In-memory SQLite async test database session."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    sessionmaker = async_sessionmaker(
        bind=engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )

    async with sessionmaker() as session:
        yield session

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


@pytest_asyncio.fixture
async def fake_redis() -> AsyncGenerator[Redis, None]:
    """In-memory fakeredis client for fast and isolated unit/integration tests."""
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    yield client
    await client.flushall()
    await client.aclose()


@pytest_asyncio.fixture
async def valid_api_key(test_db_session: AsyncSession) -> tuple[ApiKey, str]:
    """Create and return a valid test API key and its plaintext string."""
    raw_key, key_hash, key_prefix = generate_api_key()
    repo = ApiKeyRepository(test_db_session)
    db_key = await repo.create(
        name="Integration Test Key",
        key_hash=key_hash,
        key_prefix=key_prefix,
        requests_per_minute=60,
    )
    return db_key, raw_key


@pytest_asyncio.fixture
async def client(
    test_db_session: AsyncSession, fake_redis: Redis
) -> AsyncGenerator[AsyncClient, None]:
    """Async HTTP client bound to the FastAPI application with test DB & Redis overrides."""
    app = create_app()

    async def override_get_db():
        yield test_db_session

    async def override_get_redis():
        yield fake_redis

    app.dependency_overrides[get_db_session] = override_get_db
    app.dependency_overrides[get_redis] = override_get_redis

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
