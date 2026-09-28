import asyncio
import hashlib
import json
import secrets
from datetime import UTC, datetime, timedelta

import pytest
import respx
from conftest import override_get_db
from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy.ext.asyncio import async_sessionmaker
from stripe import WebhookSignature

from app.api.deps import get_db
from app.core.config import settings
from app.main import create_app
from app.models.session import Session
from app.models.stripe_webhook_event import StripeWebhookEvent
from app.models.user import User

WEBHOOK_SECRET = "whsec_test_secret"


def _signed_payload(event_id: str, wallet_id: str = "wallet-1", amount_cents=5000):
    payload = json.dumps(
        {
            "id": event_id,
            "type": "payment_intent.succeeded",
            "data": {
                "object": {"amount": amount_cents, "metadata": {"wallet_id": wallet_id}}
            },
        }
    ).encode()
    sig = WebhookSignature.generate_signature_header(payload.decode(), WEBHOOK_SECRET)
    return payload, sig


@pytest.fixture
async def app_and_token(db_session):
    user = User(
        work_email="a@example.com",
        ms_oid="oid-1",
        ez_wallet_id="wallet-1",
        workspace_account_status="active",
    )
    db_session.add(user)
    await db_session.flush()

    raw_token = secrets.token_urlsafe(32)
    db_session.add(
        Session(
            user_id=user.id,
            token_hash=hashlib.sha256(raw_token.encode()).hexdigest(),
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
    )
    await db_session.flush()

    app = create_app()
    app.dependency_overrides[get_db] = override_get_db(db_session)
    return app, raw_token, user


@pytest.mark.asyncio
@respx.mock
async def test_get_wallet_active_returns_passthrough_balance(app_and_token):
    app, token, _user = app_and_token
    respx.get("https://workspace.invalid/wallet/wallet-1/balance").mock(
        return_value=Response(200, json={"credit_balance": 42})
    )
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get(
            "/wallet", headers={"Authorization": f"Bearer {token}"}
        )
    assert response.status_code == 200
    assert response.json() == {"credit_balance": 42}


@pytest.mark.asyncio
async def test_get_wallet_unprovisioned_returns_503_no_workspace_call(db_session):
    user = User(work_email="b@example.com", ms_oid="oid-2")  # default unprovisioned
    db_session.add(user)
    await db_session.flush()
    raw_token = secrets.token_urlsafe(32)
    db_session.add(
        Session(
            user_id=user.id,
            token_hash=hashlib.sha256(raw_token.encode()).hexdigest(),
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
    )
    await db_session.flush()

    app = create_app()
    app.dependency_overrides[get_db] = override_get_db(db_session)

    with respx.mock:  # no routes registered -- any call would raise
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get(
                "/wallet", headers={"Authorization": f"Bearer {raw_token}"}
            )

    assert response.status_code == 503
    assert response.json()["error_code"] == "workspace_account_unprovisioned"


@pytest.mark.asyncio
@respx.mock
async def test_topup_creates_payment_intent(app_and_token, monkeypatch):
    app, token, _user = app_and_token
    monkeypatch.setattr(
        "app.api.wallet.stripe_service.create_payment_intent",
        lambda amount_usd, wallet_id: "pi_secret_123",
    )
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/wallet/topup",
            json={"amount_usd": 50},
            headers={"Authorization": f"Bearer {token}"},
        )
    assert response.status_code == 200
    assert response.json() == {"client_secret": "pi_secret_123"}


@pytest.mark.asyncio
async def test_topup_rejects_non_positive_amount_returns_422(app_and_token):
    app, token, _user = app_and_token
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/wallet/topup",
            json={"amount_usd": 0},
            headers={"Authorization": f"Bearer {token}"},
        )
    assert response.status_code == 422


@pytest.fixture(autouse=True)
def stripe_webhook_secret(monkeypatch):
    monkeypatch.setattr(settings, "stripe_webhook_secret", WEBHOOK_SECRET)


@pytest.mark.asyncio
@respx.mock
async def test_webhook_route_valid_unseen_event_credits_and_returns_200(db_session):
    respx.post("https://workspace.invalid/wallet/wallet-1/credit").mock(
        return_value=Response(200, json={"credit_balance": 150, "status": "ok"})
    )
    app = create_app()
    app.dependency_overrides[get_db] = override_get_db(db_session)
    payload, sig = _signed_payload("evt_route_1")

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/webhooks/stripe",
            content=payload,
            headers={"stripe-signature": sig, "content-type": "application/json"},
        )

    assert response.status_code == 200
    record = await db_session.get(StripeWebhookEvent, "evt_route_1")
    assert record.credited is True


@pytest.mark.asyncio
async def test_webhook_route_invalid_signature_returns_400(db_session):
    app = create_app()
    app.dependency_overrides[get_db] = override_get_db(db_session)
    payload, _sig = _signed_payload("evt_route_bad")

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/webhooks/stripe",
            content=payload,
            headers={
                "stripe-signature": "t=1,v1=bad",
                "content-type": "application/json",
            },
        )

    assert response.status_code == 400


@pytest.mark.asyncio
@respx.mock
async def test_webhook_route_redelivery_returns_200_no_second_credit(db_session):
    db_session.add(StripeWebhookEvent(event_id="evt_route_dup", credited=True))
    await db_session.flush()

    credit_route = respx.post("https://workspace.invalid/wallet/wallet-1/credit")
    app = create_app()
    app.dependency_overrides[get_db] = override_get_db(db_session)
    payload, sig = _signed_payload("evt_route_dup")

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/webhooks/stripe",
            content=payload,
            headers={"stripe-signature": sig, "content-type": "application/json"},
        )

    assert response.status_code == 200
    assert credit_route.call_count == 0


@pytest.mark.asyncio
@respx.mock
async def test_webhook_route_concurrent_same_event_only_credits_once(db_engine):
    """Two truly concurrent deliveries of the same new event -- each with
    its own DB session/connection, like two real concurrent requests would
    have. The dedup check-then-insert isn't atomic, so one request's insert
    can lose the unique-constraint race: it must come back as a clean 502
    (Stripe retries) rather than corrupting state, and the credit must
    still only ever land once."""
    credit_route = respx.post("https://workspace.invalid/wallet/wallet-1/credit").mock(
        return_value=Response(200, json={"credit_balance": 150, "status": "ok"})
    )
    sessionmaker = async_sessionmaker(db_engine, expire_on_commit=False)
    session_a = sessionmaker()
    session_b = sessionmaker()

    app_a = create_app()
    app_a.dependency_overrides[get_db] = override_get_db(session_a)
    app_b = create_app()
    app_b.dependency_overrides[get_db] = override_get_db(session_b)

    payload, sig = _signed_payload("evt_concurrent")
    headers = {"stripe-signature": sig, "content-type": "application/json"}

    async def _post(app):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            return await client.post(
                "/webhooks/stripe", content=payload, headers=headers
            )

    try:
        responses = await asyncio.gather(_post(app_a), _post(app_b))
    finally:
        await session_a.close()
        await session_b.close()

    statuses = sorted(r.status_code for r in responses)
    assert statuses == [200, 502]
    assert credit_route.call_count == 1

    async with sessionmaker() as verify_session:
        record = await verify_session.get(StripeWebhookEvent, "evt_concurrent")
        assert record.credited is True
