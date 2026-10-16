"""Audit worker: claims queued jobs and runs them with bounded concurrency.

Crash safety comes from the database lease: a job whose worker dies keeps status RUNNING
with an expiring lease, and `claim_next` hands it to the next free worker, which resumes
from the stored checkpoint.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import socket
import time
from pathlib import Path

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from pca.config import Settings, get_settings
from pca.db.session import create_engine, create_sessionmaker
from pca.logging_setup import configure_logging
from pca.security.crypto import Keyring
from pca.services import audit_engine
from pca.services.connection import keyring_from_settings
from pca.telegram.types import AccountGateway
from pca.worker.notifier import BotNotifier

logger = logging.getLogger("pca.worker")

HEARTBEAT_INTERVAL_SECONDS = 20
PURGE_INTERVAL_SECONDS = 6 * 3600


async def _heartbeat(path: Path) -> None:
    while True:
        with contextlib.suppress(OSError):
            path.write_text(str(int(time.time())))
        await asyncio.sleep(HEARTBEAT_INTERVAL_SECONDS)


async def _purge_loop(sessionmaker: async_sessionmaker[AsyncSession], settings: Settings) -> None:
    while True:
        try:
            async with sessionmaker() as session:
                removed = await audit_engine.purge_expired(session, settings)
                await session.commit()
            if removed:
                logger.info("purged %d expired requester rows", removed)
        except Exception:
            logger.exception("retention purge failed")
        await asyncio.sleep(PURGE_INTERVAL_SECONDS)


async def poll_loop(
    sessionmaker: async_sessionmaker[AsyncSession],
    settings: Settings,
    keyring: Keyring,
    gateway: AccountGateway,
    notifier: audit_engine.AuditNotifier,
    worker_id: str,
    stop: asyncio.Event,
) -> None:
    slots = asyncio.Semaphore(settings.worker_concurrency)
    running: set[asyncio.Task[None]] = set()

    async def _run(job_id: int) -> None:
        try:
            await audit_engine.run_job(sessionmaker, settings, keyring, gateway, job_id, worker_id, notifier)
        except Exception:
            logger.exception("audit job crashed", extra={"audit_job_id": job_id})
        finally:
            slots.release()

    while not stop.is_set():
        await slots.acquire()
        job_id: int | None = None
        try:
            async with sessionmaker() as session:
                job = await audit_engine.claim_next(session, settings, worker_id)
                job_id = job.id if job else None
                await session.commit()
        except Exception:
            logger.exception("claiming a job failed")
        if job_id is None:
            slots.release()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), settings.worker_poll_seconds)
            continue
        logger.info("claimed audit job", extra={"audit_job_id": job_id})
        task = asyncio.create_task(_run(job_id))
        running.add(task)
        task.add_done_callback(running.discard)

    # Leave in-flight jobs to finish their current page; anything cut off resumes on lease expiry.
    if running:
        await asyncio.wait(running, timeout=30)


async def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    if not (settings.telegram_api_id and settings.telegram_api_hash.get_secret_value()):
        raise SystemExit("PCA_TELEGRAM_API_ID / PCA_TELEGRAM_API_HASH are required for the worker")
    if not settings.session_keys_raw.get_secret_value():
        raise SystemExit("session keys are required for the worker")

    from pca.telegram.gateway import TelethonGateway

    engine = create_engine(settings.database_url)
    sessionmaker = create_sessionmaker(engine)
    keyring = keyring_from_settings(settings)
    gateway = TelethonGateway(settings)
    bot = Bot(settings.bot_token.get_secret_value(), default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    notifier = BotNotifier(bot, sessionmaker)
    worker_id = f"{socket.gethostname()}:{os.getpid()}"

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)

    background = [
        asyncio.create_task(_heartbeat(Path(settings.worker_heartbeat_path))),
        asyncio.create_task(_purge_loop(sessionmaker, settings)),
    ]
    logger.info("worker %s started with concurrency %d", worker_id, settings.worker_concurrency)
    try:
        await poll_loop(sessionmaker, settings, keyring, gateway, notifier, worker_id, stop)
    finally:
        for task in background:
            task.cancel()
        await bot.session.close()
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
