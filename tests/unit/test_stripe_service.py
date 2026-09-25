import json
import time
from unittest.mock import AsyncMock

import pytest
from stripe import WebhookSignature

from app.core.config import settings
from app.models.stripe_webhook_event import StripeWebhookEvent
from app.services import stripe_service

WEBHOOK_SECRET = "whsec_test_secret"


@pytest.fixture(autouse=True)
def stripe_settings(monkeypatch):
    monkeypatch.setattr(settings, "stripe_webhook_secret", WEBHOOK_SECRET)


def _make_event_payload(
    event_id: str = "evt_1", amount_cents: int = 5000, wallet_id: str = "wallet-1"
) -> bytes:
    return json.dumps(
        {
            "id": event_id,
            "type": "payment_intent.succeeded",
            "data": {
                "object": {
                    "amount": amount_cents,
                    "metadata": {"wallet_id": wallet_id},
                }
            },
        }
    ).encode()


def _sign(payload: bytes, *, timestamp: int | None = None) -> str:
    return WebhookSignature.generate_signature_header(
        payload.decode(), WEBHOOK_SECRET, timestamp=timestamp
    )


def test_construct_event_accepts_valid_signature():
    payload = _make_event_payload()
    sig = _sign(payload)
    event = stripe_service.construct_event(payload, sig)
    assert event["id"] == "evt_1"


def test_construct_event_rejects_invalid_signature():
    payload = _make_event_payload()
    with pytest.raises(stripe_service.InvalidStripeSignatureError):
        stripe_service.construct_event(payload, "t=1,v1=not-a-real-signature")


def test_construct_event_rejects_stale_timestamp():
    payload = _make_event_payload()
    stale_sig = _sign(payload, timestamp=int(time.time()) - 3600)
    with pytest.raises(stripe_service.InvalidStripeSignatureError):
        stripe_service.construct_event(payload, stale_sig)


@pytest.mark.anyio
async def test_handle_payment_intent_succeeded_credits_and_records_event(db_session):
    payload = _make_event_payload(event_id="evt_happy")
    event = stripe_service.construct_event(payload, _sign(payload))
    workspace_client = AsyncMock()
    workspace_client.credit.return_value = {"credit_balance": 150, "status": "ok"}

    await stripe_service.handle_payment_intent_succeeded(
        event, db_session, workspace_client
    )

    workspace_client.credit.assert_awaited_once_with(
        "wallet-1", 50, idempotency_key="evt_happy", reason="topup"
    )
    record = await db_session.get(StripeWebhookEvent, "evt_happy")
    assert record.credited is True


@pytest.mark.anyio
async def test_redelivery_of_credited_event_does_not_credit_again(db_session):
    payload = _make_event_payload(event_id="evt_dup")
    event = stripe_service.construct_event(payload, _sign(payload))
    db_session.add(StripeWebhookEvent(event_id="evt_dup", credited=True))
    await db_session.flush()

    workspace_client = AsyncMock()
    await stripe_service.handle_payment_intent_succeeded(
        event, db_session, workspace_client
    )

    workspace_client.credit.assert_not_awaited()


@pytest.mark.anyio
async def test_credit_failure_leaves_row_credited_false(db_session):
    payload = _make_event_payload(event_id="evt_fail")
    event = stripe_service.construct_event(payload, _sign(payload))
    workspace_client = AsyncMock()
    workspace_client.credit.side_effect = RuntimeError("workspace unreachable")

    with pytest.raises(RuntimeError):
        await stripe_service.handle_payment_intent_succeeded(
            event, db_session, workspace_client
        )

    record = await db_session.get(StripeWebhookEvent, "evt_fail")
    assert record.credited is False


@pytest.mark.anyio
async def test_retry_after_partial_failure_still_credits(db_session):
    """A retry for an event whose row is still credited=false must recover
    the credit rather than losing it permanently (KD12)."""
    payload = _make_event_payload(event_id="evt_retry")
    event = stripe_service.construct_event(payload, _sign(payload))
    workspace_client = AsyncMock()
    workspace_client.credit.side_effect = [RuntimeError("boom"), {"status": "ok"}]

    with pytest.raises(RuntimeError):
        await stripe_service.handle_payment_intent_succeeded(
            event, db_session, workspace_client
        )
    await stripe_service.handle_payment_intent_succeeded(
        event, db_session, workspace_client
    )

    record = await db_session.get(StripeWebhookEvent, "evt_retry")
    assert record.credited is True
    assert workspace_client.credit.await_count == 2
