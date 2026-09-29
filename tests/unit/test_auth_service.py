from types import SimpleNamespace
from unittest.mock import AsyncMock

import jwt
import pytest

from app.core.errors import UnauthorizedError
from app.services import auth_service


@pytest.fixture(autouse=True)
def patch_jwks(monkeypatch, rsa_keypair):
    _private_key, public_key = rsa_keypair
    monkeypatch.setattr(
        auth_service._jwks_client,
        "get_signing_key_from_jwt",
        lambda _token: SimpleNamespace(key=public_key),
    )


def test_validate_entra_token_accepts_valid_token(make_entra_token):
    token = make_entra_token()
    claims = auth_service.validate_entra_token(token)
    assert claims["oid"] == "user-oid-1"


def test_validate_entra_token_rejects_wrong_tenant(make_entra_token):
    token = make_entra_token(tenant_id="some-other-tenant")
    with pytest.raises(UnauthorizedError):
        auth_service.validate_entra_token(token)


def test_validate_entra_token_rejects_expired_token(make_entra_token):
    token = make_entra_token(expired=True)
    with pytest.raises(UnauthorizedError):
        auth_service.validate_entra_token(token)


def test_validate_entra_token_rejects_malformed_token():
    with pytest.raises(UnauthorizedError):
        auth_service.validate_entra_token("not-a-real-jwt")


class _FakeConfidentialClientApplication:
    """MSAL's real class performs a live OIDC authority-discovery HTTP call
    in __init__ -- patching just acquire_token_on_behalf_of on the real
    class still hits the network before that method is ever reached, since
    a fake test tenant genuinely doesn't exist on Microsoft's servers.
    Replacing the whole class is the actual mock boundary.
    """

    def __init__(self, *args, **kwargs):
        pass


def test_exchange_obo_success(monkeypatch, make_entra_token):
    token = make_entra_token()
    claims = jwt.decode(token, options={"verify_signature": False})
    _FakeConfidentialClientApplication.acquire_token_on_behalf_of = (
        lambda self, user_assertion, scopes: {"access_token": "exchanged-token"}
    )
    monkeypatch.setattr(
        "app.services.auth_service.msal.ConfidentialClientApplication",
        _FakeConfidentialClientApplication,
    )
    result = auth_service.exchange_obo(token, claims)
    assert result == "exchanged-token"


def test_exchange_obo_failure_raises_unauthorized(monkeypatch, make_entra_token):
    token = make_entra_token()
    claims = jwt.decode(token, options={"verify_signature": False})
    _FakeConfidentialClientApplication.acquire_token_on_behalf_of = (
        lambda self, user_assertion, scopes: {
            "error": "interaction_required",
            "error_description": "MFA step-up required",
        }
    )
    monkeypatch.setattr(
        "app.services.auth_service.msal.ConfidentialClientApplication",
        _FakeConfidentialClientApplication,
    )
    with pytest.raises(UnauthorizedError):
        auth_service.exchange_obo(token, claims)


@pytest.mark.anyio
async def test_handle_auth_session_legacy_mode_succeeds(
    monkeypatch, make_entra_token, db_session
):
    """Regression: exchange_obo requests the OBO token against scope
    api://{client_id}/access_as_user, so its aud claim is api://{client_id}
    -- re-validating it against the bare client ID (the incoming token's
    own audience) always failed. Legacy mode had zero test coverage, which
    is exactly how this shipped unnoticed."""
    from app.core.config import settings

    incoming_token = make_entra_token()
    obo_token = make_entra_token(aud=f"api://{settings.entra_client_id}")

    _FakeConfidentialClientApplication.acquire_token_on_behalf_of = (
        lambda self, user_assertion, scopes: {"access_token": obo_token}
    )
    monkeypatch.setattr(
        "app.services.auth_service.msal.ConfidentialClientApplication",
        _FakeConfidentialClientApplication,
    )

    workspace_client = AsyncMock()
    workspace_client.lookup_or_login_or_create.return_value = {
        "workspace_account_id": "wsacct-1",
        "wallet_id": "wallet-1",
        "credit_balance": 0,
        "status": "created",
    }

    user, raw_token = await auth_service.handle_auth_session(
        incoming_token, "legacy", db_session, workspace_client
    )

    assert raw_token
    assert user.workspace_account_status == "active"


@pytest.mark.anyio
async def test_handle_auth_session_dev_mode_succeeds_with_correct_token(
    monkeypatch, db_session
):
    from app.core.config import settings

    monkeypatch.setattr(settings, "dev_auth_token", "test-dev-secret")

    workspace_client = AsyncMock()
    workspace_client.lookup_or_login_or_create.return_value = {
        "workspace_account_id": "wsacct-dev",
        "wallet_id": "wallet-dev",
        "credit_balance": 0,
        "status": "created",
    }

    user, raw_token = await auth_service.handle_auth_session(
        "test-dev-secret", "dev", db_session, workspace_client
    )

    assert raw_token
    assert user.ms_oid == "dev-oid"
    assert user.workspace_account_status == "active"


@pytest.mark.anyio
async def test_handle_auth_session_dev_mode_rejects_wrong_token(
    monkeypatch, db_session
):
    from app.core.config import settings

    monkeypatch.setattr(settings, "dev_auth_token", "test-dev-secret")

    with pytest.raises(UnauthorizedError):
        await auth_service.handle_auth_session(
            "wrong-token", "dev", db_session, AsyncMock()
        )


@pytest.mark.anyio
async def test_handle_auth_session_dev_mode_disabled_by_default(
    monkeypatch, db_session
):
    from app.core.config import settings

    monkeypatch.setattr(settings, "dev_auth_token", "")

    with pytest.raises(UnauthorizedError):
        await auth_service.handle_auth_session("", "dev", db_session, AsyncMock())


@pytest.mark.anyio
async def test_handle_auth_session_first_use_provisions_workspace(
    make_entra_token, db_session
):
    token = make_entra_token()
    workspace_client = AsyncMock()
    workspace_client.lookup_or_login_or_create.return_value = {
        "workspace_account_id": "wsacct-1",
        "wallet_id": "wallet-1",
        "credit_balance": 0,
        "status": "created",
    }

    user, raw_token = await auth_service.handle_auth_session(
        token, "naa", db_session, workspace_client
    )

    assert user.workspace_account_status == "active"
    assert user.ez_wallet_id == "wallet-1"
    assert raw_token
    workspace_client.lookup_or_login_or_create.assert_awaited_once()


@pytest.mark.anyio
async def test_handle_auth_session_second_call_does_not_reprovision(
    make_entra_token, db_session
):
    token = make_entra_token()
    workspace_client = AsyncMock()
    workspace_client.lookup_or_login_or_create.return_value = {
        "workspace_account_id": "wsacct-1",
        "wallet_id": "wallet-1",
        "credit_balance": 0,
        "status": "created",
    }

    await auth_service.handle_auth_session(token, "naa", db_session, workspace_client)
    await auth_service.handle_auth_session(token, "naa", db_session, workspace_client)

    workspace_client.lookup_or_login_or_create.assert_awaited_once()


@pytest.mark.anyio
async def test_handle_auth_session_workspace_timeout_stays_unprovisioned(
    make_entra_token, db_session
):
    token = make_entra_token()
    workspace_client = AsyncMock()
    workspace_client.lookup_or_login_or_create.side_effect = TimeoutError()

    user, raw_token = await auth_service.handle_auth_session(
        token, "naa", db_session, workspace_client
    )

    assert user.workspace_account_status == "unprovisioned"
    assert raw_token  # R1: session still succeeds even when Workspace fails


@pytest.mark.anyio
async def test_handle_auth_session_retries_provisioning_on_next_call(
    make_entra_token, db_session
):
    """R4/KD4: a user stuck at workspace_account_status="unprovisioned"
    after a failed first attempt must have provisioning re-attempted on
    their next /auth/session call, not just on their literal first-ever
    call.
    """
    token = make_entra_token()
    workspace_client = AsyncMock()
    workspace_client.lookup_or_login_or_create.side_effect = TimeoutError()

    user, _raw_token = await auth_service.handle_auth_session(
        token, "naa", db_session, workspace_client
    )
    assert user.workspace_account_status == "unprovisioned"

    workspace_client.lookup_or_login_or_create.side_effect = None
    workspace_client.lookup_or_login_or_create.return_value = {
        "workspace_account_id": "wsacct-1",
        "wallet_id": "wallet-1",
        "credit_balance": 0,
        "status": "created",
    }

    user, raw_token = await auth_service.handle_auth_session(
        token, "naa", db_session, workspace_client
    )

    assert user.workspace_account_status == "active"
    assert user.ez_wallet_id == "wallet-1"
    assert raw_token
    assert workspace_client.lookup_or_login_or_create.await_count == 2
