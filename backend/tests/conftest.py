from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from pca.db.models import AuditJob, Base, Channel, User
from pca.services import users as user_service

BACKEND_DIR = Path(__file__).resolve().parents[1]
TEST_DB = "pca_test"


@pytest.fixture(scope="session")
def database_url() -> Iterator[str]:
    """Real PostgreSQL: PCA_TEST_DATABASE_URL if provided, otherwise an embedded server."""
    url = os.environ.get("PCA_TEST_DATABASE_URL")
    if url is None:
        pgserver = pytest.importorskip("pgserver")
        server = pgserver.get_server("/tmp/pca_pg", cleanup_mode=None)
        server.psql(f"DROP DATABASE IF EXISTS {TEST_DB};")
        server.psql(f"CREATE DATABASE {TEST_DB};")
        url = server.get_uri(TEST_DB).replace("postgresql://", "postgresql+asyncpg://", 1)

    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_DIR / "migrations"))
    config.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    command.upgrade(config, "head")
    yield url


@pytest.fixture(scope="session")
async def engine(database_url: str) -> AsyncIterator[AsyncEngine]:
    eng = create_async_engine(database_url, poolclass=NullPool)
    yield eng
    await eng.dispose()


@pytest.fixture
async def sessionmaker(engine: AsyncEngine) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    tables = ", ".join(t.name for t in Base.metadata.sorted_tables)
    async with engine.begin() as conn:
        await conn.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
    yield async_sessionmaker(engine, expire_on_commit=False)


@pytest.fixture
async def session(sessionmaker) -> AsyncIterator[AsyncSession]:
    async with sessionmaker() as s:
        yield s


async def make_user(sm, telegram_user_id: int, *, admin: bool = False) -> User:
    async with sm() as s, s.begin():
        return await user_service.register_user(
            s,
            telegram_user_id=telegram_user_id,
            username=f"user{telegram_user_id}",
            bootstrap_admin_ids=[telegram_user_id] if admin else [],
        )


async def make_audit_job(sm, user_id: int, telegram_channel_id: int = -1001) -> int:
    async with sm() as s, s.begin():
        channel = Channel(telegram_channel_id=telegram_channel_id, title="Test channel")
        s.add(channel)
        await s.flush()
        job = AuditJob(user_id=user_id, channel_id=channel.id)
        s.add(job)
        await s.flush()
        return job.id
