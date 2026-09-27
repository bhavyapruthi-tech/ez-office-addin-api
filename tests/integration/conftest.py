import asyncio
import os

import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import settings

# Verification Contract: integration tests require a local Postgres, e.g.
# `docker compose up -d db`. When unreachable, skip rather than error --
# these tests are honest about needing infrastructure this environment may
# not have (see plan Assumptions).
TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL", settings.database_url)


@pytest_asyncio.fixture
async def db_engine():
    engine = create_async_engine(TEST_DATABASE_URL)
    try:
        async with engine.connect():
            pass
    except Exception as exc:  # noqa: BLE001 -- environment probe, not app logic
        await engine.dispose()
        pytest.skip(f"Postgres not reachable at {TEST_DATABASE_URL}: {exc}")

    alembic_cfg = Config("alembic.ini")
    alembic_cfg.set_main_option("sqlalchemy.url", TEST_DATABASE_URL)
    # env.py's async template calls asyncio.run() internally, which cannot
    # nest inside this fixture's own running event loop -- run it in a
    # separate thread instead.
    await asyncio.to_thread(command.upgrade, alembic_cfg, "head")

    yield engine

    await asyncio.to_thread(command.downgrade, alembic_cfg, "base")
    await engine.dispose()


@pytest_asyncio.fixture
async def db_session(db_engine):
    sessionmaker = async_sessionmaker(db_engine, expire_on_commit=False)
    async with sessionmaker() as session:
        yield session


def override_get_db(session):
    """get_db is an async generator; FastAPI only recognizes the generator
    protocol on the override callable itself. A plain `lambda: iter([session])`
    returns that iterator as-is, so `db` ends up bound to the raw iterator
    instead of the session."""

    async def _override():
        yield session

    return _override
