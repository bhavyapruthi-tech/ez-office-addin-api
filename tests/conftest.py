import time

import jwt
import pytest
import pytest_asyncio
from cryptography.hazmat.primitives.asymmetric import rsa
from httpx import ASGITransport, AsyncClient

from app.core.config import settings

TEST_TENANT_ID = "test-tenant-id"
TEST_CLIENT_ID = "test-client-id"


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


@pytest.fixture(autouse=True)
def entra_settings(monkeypatch):
    """No real Entra ID app registration exists yet (see plan Assumptions)
    -- tests exercise the validator/OBO logic against these placeholder
    values rather than a live tenant.
    """
    monkeypatch.setattr(settings, "entra_tenant_id", TEST_TENANT_ID)
    monkeypatch.setattr(settings, "entra_client_id", TEST_CLIENT_ID)
    monkeypatch.setattr(settings, "entra_client_secret", "test-client-secret")


@pytest.fixture(scope="session")
def rsa_keypair():
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return private_key, private_key.public_key()


@pytest.fixture
def make_entra_token(rsa_keypair):
    private_key, _public_key = rsa_keypair

    def _make(
        *,
        tenant_id: str | None = None,
        aud: str | None = None,
        oid: str = "user-oid-1",
        expired: bool = False,
    ) -> str:
        now = int(time.time())
        claims = {
            "oid": oid,
            "tid": tenant_id or settings.entra_tenant_id,
            "aud": aud or settings.entra_client_id,
            "iss": (
                f"https://login.microsoftonline.com/"
                f"{tenant_id or settings.entra_tenant_id}/v2.0"
            ),
            "preferred_username": "user@example.com",
            "iat": now - 3600 if expired else now,
            "exp": now - 1 if expired else now + 3600,
        }
        return jwt.encode(
            claims, private_key, algorithm="RS256", headers={"kid": "test-kid"}
        )

    return _make
