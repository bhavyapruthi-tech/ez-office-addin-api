from typing import Any

import stripe
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.stripe_webhook_event import StripeWebhookEvent
from app.services.workspace_client import WorkspaceClient

stripe.api_key = settings.stripe_secret_key


class InvalidStripeSignatureError(Exception):
    pass


def create_payment_intent(amount_usd: int, wallet_id: str) -> str:
    # Metadata carries the wallet to credit at webhook time -- there's no
    # local ledger row to look this up from otherwise (ARCHITECTURE.md sec4).
    intent = stripe.PaymentIntent.create(
        amount=amount_usd * 100,
        currency="usd",
        metadata={"wallet_id": wallet_id},
    )
    return str(intent.client_secret)


def construct_event(payload: bytes, sig_header: str) -> stripe.Event:
    """R6: raw body, never a re-serialized `request.json()` -- Stripe's
    signature covers the exact bytes sent. Relies on `construct_event`'s
    default signature-timestamp tolerance (KD12) to reject stale replayed
    events; this must never be widened or bypassed.
    """
    try:
        return stripe.Webhook.construct_event(
            payload, sig_header, settings.stripe_webhook_secret
        )
    except (ValueError, stripe.SignatureVerificationError) as exc:
        raise InvalidStripeSignatureError(str(exc)) from None


async def handle_payment_intent_succeeded(
    event: stripe.Event,
    db: AsyncSession,
    workspace_client: WorkspaceClient,
) -> None:
    """KD12: dedup keyed on `credited` status, not mere row existence, so a
    retry after a partial failure (row inserted, credit call then failed)
    still re-attempts the credit rather than being silently swallowed.
    """
    event_id = event["id"]
    payment_intent = event["data"]["object"]
    wallet_id = payment_intent["metadata"]["wallet_id"]
    amount_usd = payment_intent["amount"] // 100

    result = await db.execute(
        select(StripeWebhookEvent).where(StripeWebhookEvent.event_id == event_id)
    )
    record = result.scalar_one_or_none()

    if record is not None and record.credited:
        return  # already credited -- true redelivery, no-op

    if record is None:
        record = StripeWebhookEvent(event_id=event_id, credited=False)
        db.add(record)
        await db.flush()

    result_body: dict[str, Any] = await workspace_client.credit(
        wallet_id, amount_usd, idempotency_key=event_id, reason="topup"
    )
    if result_body.get("status") != "ok":
        raise RuntimeError(f"Workspace credit failed: {result_body}")

    record.credited = True
    await db.flush()
