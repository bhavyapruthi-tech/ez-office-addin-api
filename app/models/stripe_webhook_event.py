from datetime import datetime

from sqlalchemy import Boolean, DateTime, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class StripeWebhookEvent(Base):
    """KD12: dedup keyed on `credited` status, not mere row existence.

    The 200 short-circuit on webhook redelivery checks `credited=True`;
    a row with `credited=False` means a prior credit attempt crashed or
    failed, and a redelivery should re-attempt it, not short-circuit.
    """

    __tablename__ = "stripe_webhook_events"

    event_id: Mapped[str] = mapped_column(String, primary_key=True)
    credited: Mapped[bool] = mapped_column(Boolean, default=False)
    processed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
