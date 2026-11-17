"""SQLAlchemy database models for API keys and usage logging."""

import uuid
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    """Base declarative class for all SQLAlchemy models."""

    pass


class ApiKey(Base):
    """Client API key authentication credentials and rate limit/budget policy."""

    __tablename__ = "api_keys"

    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=lambda: str(uuid.uuid4()),
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    # Only store SHA-256 hash of the API key
    key_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    # Masked prefix for identification in admin UI (e.g., "gw-live-***a1b2")
    key_prefix: Mapped[str] = mapped_column(String(16), nullable=False)

    requests_per_minute: Mapped[int] = mapped_column(Integer, default=60, nullable=False)
    monthly_budget_usd: Mapped[Decimal] = mapped_column(
        Numeric(10, 4),
        default=Decimal("100.0000"),
        nullable=False,
    )

    is_revoked: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    isolate_cache: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        nullable=False,
        doc="If True, isolate cache entries to this key rather than sharing across clients",
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        nullable=False,
    )

    usage_logs: Mapped[list["UsageLog"]] = relationship(
        "UsageLog",
        back_populates="api_key",
        cascade="all, delete-orphan",
    )


class UsageLog(Base):
    """Audit log capturing per-request metrics, token counts, cost, and routing outcomes."""

    __tablename__ = "usage_logs"

    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=lambda: str(uuid.uuid4()),
    )
    request_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    api_key_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("api_keys.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        nullable=False,
        index=True,
    )

    alias: Mapped[str | None] = mapped_column(String(50), nullable=True, index=True)
    requested_model: Mapped[str] = mapped_column(String(100), nullable=False)
    provider: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    model: Mapped[str] = mapped_column(String(100), nullable=False, index=True)

    input_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    cost_usd: Mapped[Decimal] = mapped_column(
        Numeric(12, 6),
        default=Decimal("0.000000"),
        nullable=False,
    )
    latency_ms: Mapped[float] = mapped_column(Numeric(10, 2), default=0.0, nullable=False)

    cache_hit: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    fallback_used: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    status_code: Mapped[int] = mapped_column(Integer, default=200, nullable=False)
    error_type: Mapped[str | None] = mapped_column(String(100), nullable=True)
    streaming: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    estimated_tokens: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        nullable=False,
        doc="True if token count was estimated for streaming responses lacking usage metadata",
    )

    # Privacy preservation: by default only store payload hashes
    prompt_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    response_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    prompt_payload: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        doc="Only populated if LOG_PAYLOADS=True is explicitly configured",
    )
    response_payload: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        doc="Only populated if LOG_PAYLOADS=True is explicitly configured",
    )

    api_key: Mapped[ApiKey | None] = relationship("ApiKey", back_populates="usage_logs")

    __table_args__ = (
        Index("idx_usage_created_at_api_key", "created_at", "api_key_id"),
        Index("idx_usage_provider_model", "provider", "model"),
    )
