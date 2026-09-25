import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.models import Base


@pytest_asyncio.fixture
async def db_engine():
    """In-memory SQLite for unit tests that touch the ORM but don't need
    real Postgres-specific behavior (that's what tests/integration is for).
    Models use the dialect-portable `sqlalchemy.Uuid` type specifically so
    this works -- see app/models/*.py.

    `StaticPool` forces every checkout to reuse the same single connection.
    Without it, each pooled connection to `:memory:` SQLite is its own
    separate, empty database -- some job_service tests deliberately build a
    second sessionmaker from this engine to simulate a concurrent request
    or the sweep racing an in-flight task, and need to see the same data a
    session from this engine committed.
    """
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    yield engine

    await engine.dispose()


@pytest_asyncio.fixture
async def db_session(db_engine):
    sessionmaker = async_sessionmaker(db_engine, expire_on_commit=False)
    async with sessionmaker() as session:
        yield session
