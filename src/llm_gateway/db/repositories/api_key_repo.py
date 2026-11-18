"""Database repository for ApiKey persistence and queries."""

from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from llm_gateway.db.models import ApiKey


class ApiKeyRepository:
    """Encapsulates database operations on the api_keys table."""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def create(
        self,
        name: str,
        key_hash: str,
        key_prefix: str,
        requests_per_minute: int = 60,
        monthly_budget_usd: Decimal = Decimal("100.0000"),
        isolate_cache: bool = False,
    ) -> ApiKey:
        """Insert a newly issued API key record."""
        api_key = ApiKey(
            name=name,
            key_hash=key_hash,
            key_prefix=key_prefix,
            requests_per_minute=requests_per_minute,
            monthly_budget_usd=monthly_budget_usd,
            isolate_cache=isolate_cache,
            is_revoked=False,
        )
        self.session.add(api_key)
        await self.session.flush()
        await self.session.refresh(api_key)
        return api_key

    async def get_by_hash(self, key_hash: str) -> ApiKey | None:
        """Fetch API key by its SHA-256 hash."""
        stmt = select(ApiKey).where(ApiKey.key_hash == key_hash)
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_by_id(self, key_id: str) -> ApiKey | None:
        """Fetch API key by its UUID primary key."""
        stmt = select(ApiKey).where(ApiKey.id == key_id)
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def list_all(self, limit: int = 100, offset: int = 0) -> tuple[list[ApiKey], int]:
        """List all keys ordered by creation date descending with total count."""
        count_stmt = select(func.count(ApiKey.id))
        total = (await self.session.execute(count_stmt)).scalar_one()

        stmt = select(ApiKey).order_by(ApiKey.created_at.desc()).limit(limit).offset(offset)
        result = await self.session.execute(stmt)
        return list(result.scalars().all()), total

    async def revoke(self, key_id: str) -> ApiKey | None:
        """Mark an API key as revoked."""
        key = await self.get_by_id(key_id)
        if key:
            key.is_revoked = True
            await self.session.flush()
        return key
