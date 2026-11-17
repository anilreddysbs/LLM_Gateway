"""Initial schema for api_keys and usage_logs

Revision ID: 001_initial_schema
Revises:
Create Date: 2026-11-15 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "001_initial_schema"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 1. api_keys table
    op.create_table(
        "api_keys",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("key_hash", sa.String(length=64), nullable=False),
        sa.Column("key_prefix", sa.String(length=16), nullable=False),
        sa.Column("requests_per_minute", sa.Integer(), nullable=False, server_default="60"),
        sa.Column(
            "monthly_budget_usd",
            sa.Numeric(precision=10, scale=4),
            nullable=False,
            server_default="100.0000",
        ),
        sa.Column("is_revoked", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("isolate_cache", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_api_keys_key_hash", "api_keys", ["key_hash"], unique=True)

    # 2. usage_logs table
    op.create_table(
        "usage_logs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("request_id", sa.String(length=64), nullable=False),
        sa.Column("api_key_id", sa.String(length=36), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("alias", sa.String(length=50), nullable=True),
        sa.Column("requested_model", sa.String(length=100), nullable=False),
        sa.Column("provider", sa.String(length=50), nullable=False),
        sa.Column("model", sa.String(length=100), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("output_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "cost_usd",
            sa.Numeric(precision=12, scale=6),
            nullable=False,
            server_default="0.000000",
        ),
        sa.Column(
            "latency_ms",
            sa.Numeric(precision=10, scale=2),
            nullable=False,
            server_default="0.00",
        ),
        sa.Column("cache_hit", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("fallback_used", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("status_code", sa.Integer(), nullable=False, server_default="200"),
        sa.Column("error_type", sa.String(length=100), nullable=True),
        sa.Column("streaming", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column(
            "estimated_tokens",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column("prompt_hash", sa.String(length=64), nullable=True),
        sa.Column("response_hash", sa.String(length=64), nullable=True),
        sa.Column("prompt_payload", sa.Text(), nullable=True),
        sa.Column("response_payload", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["api_key_id"], ["api_keys.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_usage_logs_request_id", "usage_logs", ["request_id"], unique=False)
    op.create_index("ix_usage_logs_api_key_id", "usage_logs", ["api_key_id"], unique=False)
    op.create_index("ix_usage_logs_created_at", "usage_logs", ["created_at"], unique=False)
    op.create_index("ix_usage_logs_alias", "usage_logs", ["alias"], unique=False)
    op.create_index("ix_usage_logs_provider", "usage_logs", ["provider"], unique=False)
    op.create_index("ix_usage_logs_model", "usage_logs", ["model"], unique=False)
    op.create_index("idx_usage_created_at_api_key", "usage_logs", ["created_at", "api_key_id"])
    op.create_index("idx_usage_provider_model", "usage_logs", ["provider", "model"])


def downgrade() -> None:
    op.drop_table("usage_logs")
    op.drop_table("api_keys")
