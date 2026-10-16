"""Security event log: suspicious or rejected requests that administrators should be able to review."""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from pca.db.models import SecurityEvent
from pca.logging_setup import redact_any

log = logging.getLogger(__name__)

SEVERITIES = ("info", "warning", "critical")


async def record_event(
    session: AsyncSession,
    event_type: str,
    *,
    severity: str = "warning",
    user_id: int | None = None,
    source_ip: str | None = None,
    detail: dict[str, Any] | None = None,
) -> SecurityEvent:
    if severity not in SEVERITIES:
        severity = "warning"
    event = SecurityEvent(
        event_type=event_type[:48],
        severity=severity,
        user_id=user_id,
        source_ip=(source_ip or None) and source_ip[:64],
        detail=redact_any(detail or {}),
    )
    session.add(event)
    await session.flush()
    return event


async def record_event_detached(sessionmaker: async_sessionmaker[AsyncSession], event_type: str, **kwargs: Any) -> None:
    """Best-effort logging from request paths that must not fail because the event log is unavailable."""
    try:
        async with sessionmaker() as session:
            await record_event(session, event_type, **kwargs)
            await session.commit()
    except Exception:
        log.exception("could not record security event", extra={"event_type": event_type})


async def recent_events(session: AsyncSession, limit: int = 15) -> list[SecurityEvent]:
    return list(
        (await session.execute(select(SecurityEvent).order_by(SecurityEvent.id.desc()).limit(limit))).scalars()
    )
