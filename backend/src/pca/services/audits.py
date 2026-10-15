from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from pca.db.models import AuditJob, Channel


@dataclass(frozen=True)
class AuditListItem:
    job_id: int
    channel_title: str
    status: str
    created_at: datetime
    completed_at: datetime | None


async def list_user_audits(session: AsyncSession, user_id: int, limit: int = 10) -> list[AuditListItem]:
    """Owner-scoped listing; never returns another customer's jobs."""
    rows = (
        await session.execute(
            select(AuditJob.id, Channel.title, AuditJob.status, AuditJob.created_at, AuditJob.completed_at)
            .join(Channel, Channel.id == AuditJob.channel_id)
            .where(AuditJob.user_id == user_id)
            .order_by(AuditJob.id.desc())
            .limit(limit)
        )
    ).all()
    return [AuditListItem(*row) for row in rows]
