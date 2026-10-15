from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine


def create_engine(database_url: str, **kwargs) -> AsyncEngine:
    kwargs.setdefault("pool_pre_ping", True)
    kwargs.setdefault("pool_size", 10)
    kwargs.setdefault("max_overflow", 10)
    return create_async_engine(database_url, **kwargs)


def create_sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)
