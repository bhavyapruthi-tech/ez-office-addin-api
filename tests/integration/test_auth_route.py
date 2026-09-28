from types import SimpleNamespace

import pytest
import respx
from conftest import override_get_db
from httpx import ASGITransport, AsyncClient, Response

from app.api.deps import get_db
from app.core.config import settings
from app.main import create_app
from app.services import auth_service


@pytest.fixture
def app_with_test_db(db_session, monkeypatch, rsa_keypair):
    _private_key, public_key = rsa_keypair
    monkeypatch.setattr(
        auth_service._jwks_client,
        "get_signing_key_from_jwt",
        lambda _token: SimpleNamespace(key=public_key),
    )
    monkeypatch.setattr(settings, "entra_tenant_id", "test-tenant-id")
    monkeypatch.setattr(settings, "entra_client_id", "test-client-id")

    app = create_app()
    app.dependency_overrides[get_db] = override_get_db(db_session)
    return app


@pytest.mark.asyncio
@respx.mock
async def test_post_auth_session_end_to_end_naa(app_with_test_db, make_entra_token):
    respx.post("https://workspace.invalid/accounts").mock(
        return_value=Response(
            200,
            json={
                "workspace_account_id": "wsacct-1",
                "wallet_id": "wallet-1",
                "credit_balance": 0,
                "status": "created",
            },
        )
    )
    respx.get("https://workspace.invalid/wallet/wallet-1/balance").mock(
        return_value=Response(200, json={"credit_balance": 0})
    )

    token = make_entra_token()
    async with AsyncClient(
        transport=ASGITransport(app=app_with_test_db), base_url="http://test"
    ) as client:
        response = await client.post(
            "/auth/session", json={"token": token, "auth_mode": "naa"}
        )

    assert response.status_code == 200
    body = response.json()
    assert body["session_token"]
    assert body["user"]["email"] == "user@example.com"
    assert body["user"]["credit_balance"] == 0


@pytest.mark.asyncio
@respx.mock
async def test_post_auth_session_workspace_unreachable_still_succeeds(
    app_with_test_db, make_entra_token
):
    respx.post("https://workspace.invalid/accounts").mock(
        side_effect=Exception("connection refused")
    )

    token = make_entra_token()
    async with AsyncClient(
        transport=ASGITransport(app=app_with_test_db), base_url="http://test"
    ) as client:
        response = await client.post(
            "/auth/session", json={"token": token, "auth_mode": "naa"}
        )

    # R1: /auth/session still succeeds even when Workspace provisioning fails.
    assert response.status_code == 200
    assert response.json()["user"]["credit_balance"] is None
