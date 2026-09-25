import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models import Base


@pytest_asyncio.fixture
async def db_session():
    """In-memory SQLite for unit tests that touch the ORM but don't need
    real Postgres-specific behavior (that's what tests/integration is for).
    Models use the dialect-portable `sqlalchemy.Uuid` type specifically so
    this works -- see app/models/*.py.
    """
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    async with sessionmaker() as session:
        yield session

    await engine.dispose()
