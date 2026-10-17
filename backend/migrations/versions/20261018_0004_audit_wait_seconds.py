"""Audit active-time accounting.

Time an audit spends paused (Telegram flood waits, error backoff, worker downtime) is recorded
so it does not count toward the run-time limit. Additive only: one column with a server default.

Revision ID: 0004
Revises: 0003
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("audit_jobs", sa.Column("paused_seconds", sa.Integer(), server_default="0", nullable=False))


def downgrade() -> None:
    op.drop_column("audit_jobs", "paused_seconds")
