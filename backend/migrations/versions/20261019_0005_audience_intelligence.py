"""Audience intelligence.

Adds the audit's target country and coarse, non-identifying per-requester signal categories
(name writing system, username pattern, name style). Names and usernames are never stored.
Additive only: nullable or defaulted columns, so existing audits and rows are unaffected.

Revision ID: 0005
Revises: 0004
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("audit_jobs", sa.Column("target_country", sa.String(length=2), nullable=True))
    for column in ("name_script", "username_style", "name_style"):
        op.add_column(
            "audit_requesters",
            sa.Column(column, sa.String(length=16), server_default="unknown", nullable=False),
        )


def downgrade() -> None:
    for column in ("name_style", "username_style", "name_script"):
        op.drop_column("audit_requesters", column)
    op.drop_column("audit_jobs", "target_country")
