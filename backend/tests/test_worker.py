from __future__ import annotations

import asyncio

from pca.db.models import AuditJob
from pca.domain.enums import AuditJobStatus
from pca.services.audit_engine import NullNotifier
from pca.worker.__main__ import poll_loop
from tests.test_audit_engine import FakeScan, _setup, keyring, settings  # noqa: F401 (fixtures)


async def test_poll_loop_claims_and_finishes_job(sessionmaker, settings, keyring):
    _, job_id = await _setup(sessionmaker, settings, keyring, 9401, pending=15)
    stop = asyncio.Event()
    task = asyncio.create_task(
        poll_loop(sessionmaker, settings, keyring, FakeScan(15), NullNotifier(), "test-worker", stop)
    )
    for _ in range(100):
        async with sessionmaker() as session:
            job = await session.get(AuditJob, job_id)
            if job.status == AuditJobStatus.COMPLETED.value:
                break
        await asyncio.sleep(0.05)
    stop.set()
    await asyncio.wait_for(task, 10)
    assert job.status == AuditJobStatus.COMPLETED.value
    assert job.scanned_count == 15
