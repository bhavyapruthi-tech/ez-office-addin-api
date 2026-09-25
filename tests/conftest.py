import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest_asyncio.fixture
async def client_factory():
    """Returns a factory that wraps a FastAPI app in an ASGI test client.

    No real database connection is made -- SQLAlchemy's async engine is
    lazy, so tests that never touch the DB never need Postgres running.
    """

    async def _make(app):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            yield client

    return _make
