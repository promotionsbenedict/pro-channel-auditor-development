"""Phase 3+4: audit engine, reports, Paystack payments, support and security events.

Additive only: new columns are nullable or have server defaults, and widened
CHECK constraints accept every previously valid value.

Revision ID: 0003
Revises: 0002
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD_AUDIT = "'pending', 'queued', 'running', 'waiting_for_telegram', 'completed', 'failed', 'cancelled'"
_NEW_AUDIT = _OLD_AUDIT + ", 'partial'"
_OLD_TX = "'purchase', 'admin_grant', 'admin_debit', 'refund', 'audit_reserve', 'audit_settle', 'audit_release'"
_NEW_TX = _OLD_TX + ", 'promo'"


def upgrade() -> None:
    op.drop_constraint(op.f("ck_audit_jobs_status_valid"), "audit_jobs", type_="check")
    op.create_check_constraint(op.f("ck_audit_jobs_status_valid"), "audit_jobs", f"status IN ({_NEW_AUDIT})")
    op.drop_constraint(op.f("ck_credit_transactions_transaction_type_valid"), "credit_transactions", type_="check")
    op.create_check_constraint(
        op.f("ck_credit_transactions_transaction_type_valid"), "credit_transactions", f"transaction_type IN ({_NEW_TX})"
    )

    op.add_column("users", sa.Column("billing_email", sa.String(254)))

    op.add_column(
        "audit_jobs",
        sa.Column(
            "channel_authorization_id",
            sa.BigInteger(),
            sa.ForeignKey(
                "channel_authorizations.id",
                ondelete="SET NULL",
                name="fk_audit_jobs_channel_authorization_id_channel_authorizations",
            ),
        ),
    )
    op.add_column("audit_jobs", sa.Column("scan_limit", sa.Integer()))
    op.add_column("audit_jobs", sa.Column("scanned_count", sa.Integer(), server_default="0", nullable=False))
    op.add_column("audit_jobs", sa.Column("flood_waits", sa.Integer(), server_default="0", nullable=False))
    op.add_column("audit_jobs", sa.Column("locked_by", sa.String(64)))
    op.add_column("audit_jobs", sa.Column("lease_expires_at", sa.DateTime(timezone=True)))
    op.add_column("audit_jobs", sa.Column("last_progress_at", sa.DateTime(timezone=True)))

    op.add_column("payments", sa.Column("mode", sa.String(8), server_default="test", nullable=False))
    op.add_column("payments", sa.Column("authorization_url", sa.String(512)))
    op.add_column("payments", sa.Column("provider_status", sa.String(32)))
    op.add_column("payments", sa.Column("failure_reason", sa.String(64)))
    op.add_column("payments", sa.Column("last_checked_at", sa.DateTime(timezone=True)))
    op.create_unique_constraint(op.f("uq_payments_provider_transaction_id"), "payments", ["provider_transaction_id"])
    op.create_index("ix_payments_status_created", "payments", ["status", "created_at"])

    op.create_table(
        "audit_requesters",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "audit_job_id",
            sa.BigInteger(),
            sa.ForeignKey("audit_jobs.id", ondelete="CASCADE", name="fk_audit_requesters_audit_job_id_audit_jobs"),
            nullable=False,
        ),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("requester_ref", sa.String(64)),
        sa.Column("requested_at", sa.DateTime(timezone=True)),
        sa.Column("last_seen", sa.String(16), nullable=False),
        sa.Column("has_photo", sa.Boolean(), nullable=False),
        sa.Column("has_username", sa.Boolean(), nullable=False),
        sa.Column("is_premium", sa.Boolean(), nullable=False),
        sa.Column("is_deleted", sa.Boolean(), nullable=False),
        sa.Column("is_bot", sa.Boolean(), nullable=False),
        sa.Column("is_scam", sa.Boolean(), nullable=False),
        sa.Column("is_fake", sa.Boolean(), nullable=False),
        sa.Column("via_chatlist", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_audit_requesters"),
    )
    op.create_index("ix_audit_requesters_audit_job_id", "audit_requesters", ["audit_job_id"])
    op.create_index(
        "uq_audit_requesters_ref",
        "audit_requesters",
        ["audit_job_id", "requester_ref"],
        unique=True,
        postgresql_where=sa.text("requester_ref IS NOT NULL"),
    )

    op.create_table(
        "security_events",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "user_id",
            sa.BigInteger(),
            sa.ForeignKey("users.id", ondelete="SET NULL", name="fk_security_events_user_id_users"),
        ),
        sa.Column("event_type", sa.String(48), nullable=False),
        sa.Column("severity", sa.String(8), server_default="info", nullable=False),
        sa.Column("source_ip", sa.String(64)),
        sa.Column("detail", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_security_events"),
    )
    op.create_index("ix_security_events_user_id", "security_events", ["user_id"])
    op.create_index("ix_security_events_created_at", "security_events", ["created_at"])

    op.create_table(
        "support_tickets",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "user_id",
            sa.BigInteger(),
            sa.ForeignKey("users.id", ondelete="CASCADE", name="fk_support_tickets_user_id_users"),
            nullable=False,
        ),
        sa.Column("status", sa.String(16), server_default="open", nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("reply", sa.Text()),
        sa.Column(
            "replied_by_user_id",
            sa.BigInteger(),
            sa.ForeignKey("users.id", ondelete="SET NULL", name="fk_support_tickets_replied_by_user_id_users"),
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("status IN ('open', 'answered', 'closed')", name=op.f("ck_support_tickets_status_valid")),
        sa.PrimaryKeyConstraint("id", name="pk_support_tickets"),
    )
    op.create_index("ix_support_tickets_user_id", "support_tickets", ["user_id"])


def downgrade() -> None:
    op.drop_table("support_tickets")
    op.drop_table("security_events")
    op.drop_table("audit_requesters")
    op.drop_index("ix_payments_status_created", table_name="payments")
    op.drop_constraint(op.f("uq_payments_provider_transaction_id"), "payments", type_="unique")
    for column in ("last_checked_at", "failure_reason", "provider_status", "authorization_url", "mode"):
        op.drop_column("payments", column)
    for column in (
        "last_progress_at",
        "lease_expires_at",
        "locked_by",
        "flood_waits",
        "scanned_count",
        "scan_limit",
        "channel_authorization_id",
    ):
        op.drop_column("audit_jobs", column)
    op.drop_column("users", "billing_email")
    op.drop_constraint(op.f("ck_credit_transactions_transaction_type_valid"), "credit_transactions", type_="check")
    op.create_check_constraint(
        op.f("ck_credit_transactions_transaction_type_valid"), "credit_transactions", f"transaction_type IN ({_OLD_TX})"
    )
    op.drop_constraint(op.f("ck_audit_jobs_status_valid"), "audit_jobs", type_="check")
    op.create_check_constraint(op.f("ck_audit_jobs_status_valid"), "audit_jobs", f"status IN ({_OLD_AUDIT})")
