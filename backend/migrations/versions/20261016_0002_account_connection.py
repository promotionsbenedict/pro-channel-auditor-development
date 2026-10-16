"""account connection and channel discovery

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-16 10:00:00

Additive only: every new column is nullable or has a server default, so it is
safe to apply while the Phase 1 bot is running.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("telegram_connections", sa.Column("account_username", sa.String(length=64), nullable=True))
    op.add_column("telegram_connections", sa.Column("account_display_name", sa.String(length=255), nullable=True))
    op.add_column("telegram_connections", sa.Column("account_phone_masked", sa.String(length=32), nullable=True))
    op.add_column("telegram_connections", sa.Column("last_validated_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("telegram_connections", sa.Column("revocation_reason", sa.String(length=32), nullable=True))

    op.add_column("channels", sa.Column("channel_kind", sa.String(length=16), nullable=True))

    op.add_column("channel_authorizations", sa.Column("access_hash", sa.BigInteger(), nullable=True))
    op.add_column(
        "channel_authorizations",
        sa.Column("is_creator", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )
    op.add_column(
        "channel_authorizations",
        sa.Column("can_manage_join_requests", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )
    op.add_column(
        "channel_authorizations",
        sa.Column(
            "admin_rights",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
    )
    op.add_column("channel_authorizations", sa.Column("pending_requests", sa.Integer(), nullable=True))
    op.add_column("channel_authorizations", sa.Column("selected_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        "channel_authorizations",
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )
    op.create_index(
        "ix_channel_authorizations_connection",
        "channel_authorizations",
        ["telegram_connection_id"],
        unique=False,
    )

    op.create_table(
        "connect_flows",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("flow_id", sa.String(length=32), nullable=False),
        sa.Column("link_token_hash", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(length=24), server_default="issued", nullable=False),
        sa.Column("phone_masked", sa.String(length=32), nullable=True),
        sa.Column("encrypted_state", sa.LargeBinary(), nullable=True),
        sa.Column("encryption_key_id", sa.String(length=32), nullable=True),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("codes_sent", sa.Integer(), server_default="0", nullable=False),
        sa.Column("failure_reason", sa.String(length=32), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "status IN ('issued', 'code_sent', 'password_required', 'completed', 'failed', 'cancelled', 'expired')",
            name=op.f("ck_connect_flows_status_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_connect_flows_user_id_users"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_connect_flows")),
        sa.UniqueConstraint("flow_id", name=op.f("uq_connect_flows_flow_id")),
        sa.UniqueConstraint("link_token_hash", name=op.f("uq_connect_flows_link_token_hash")),
    )
    op.create_index(op.f("ix_connect_flows_user_id"), "connect_flows", ["user_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_connect_flows_user_id"), table_name="connect_flows")
    op.drop_table("connect_flows")
    op.drop_index("ix_channel_authorizations_connection", table_name="channel_authorizations")
    for column in (
        "updated_at",
        "selected_at",
        "pending_requests",
        "admin_rights",
        "can_manage_join_requests",
        "is_creator",
        "access_hash",
    ):
        op.drop_column("channel_authorizations", column)
    op.drop_column("channels", "channel_kind")
    for column in (
        "revocation_reason",
        "last_validated_at",
        "account_phone_masked",
        "account_display_name",
        "account_username",
    ):
        op.drop_column("telegram_connections", column)
