from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db
from app.services import stripe_service
from app.services.workspace_client import workspace_client

router = APIRouter()


@router.post("/webhooks/stripe")
async def stripe_webhook(
    request: Request, db: AsyncSession = Depends(get_db)
) -> Response:
    payload = await request.body()
    sig_header = request.headers.get("stripe-signature", "")

    try:
        event = stripe_service.construct_event(payload, sig_header)
    except stripe_service.InvalidStripeSignatureError:
        return Response(status_code=400)

    if event["type"] != "payment_intent.succeeded":
        return Response(status_code=200)

    try:
        await stripe_service.handle_payment_intent_succeeded(
            event, db, workspace_client
        )
    except Exception:
        # KD12: non-2xx so Stripe retries -- never swallow into a 200, and
        # never mark the dedup row credited=true on failure.
        await db.rollback()
        return Response(status_code=502)

    await db.commit()
    return Response(status_code=200)
