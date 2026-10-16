from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from pca.db.models import AuditJob, Channel
from pca.domain.enums import REPORTABLE_AUDIT_STATUSES


@dataclass(frozen=True)
class AuditListItem:
    job_id: int
    channel_title: str
    status: str
    created_at: datetime
    completed_at: datetime | None


async def list_user_audits(
    session: AsyncSession,
    user_id: int,
    limit: int = 10,
    *,
    reportable_only: bool = False,
    completed_only: bool = False,
    exclude_job_id: int | None = None,
) -> list[AuditListItem]:
    """Owner-scoped listing; never returns another customer's jobs."""
    stmt = (
        select(AuditJob.id, Channel.title, AuditJob.status, AuditJob.created_at, AuditJob.completed_at)
        .join(Channel, Channel.id == AuditJob.channel_id)
        .where(AuditJob.user_id == user_id)
    )
    if completed_only:
        stmt = stmt.where(AuditJob.status == "completed", AuditJob.summary.is_not(None))
    elif reportable_only:
        stmt = stmt.where(AuditJob.status.in_(REPORTABLE_AUDIT_STATUSES), AuditJob.summary.is_not(None))
    if exclude_job_id is not None:
        stmt = stmt.where(AuditJob.id != exclude_job_id)
    rows = (await session.execute(stmt.order_by(AuditJob.id.desc()).limit(limit))).all()
    return [AuditListItem(*row) for row in rows]
