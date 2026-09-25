import uuid
from datetime import datetime

from sqlalchemy import DateTime, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    work_email: Mapped[str] = mapped_column(String, unique=True, index=True)
    ms_oid: Mapped[str] = mapped_column(String, unique=True, index=True)

    ez_workspace_account_id: Mapped[str | None] = mapped_column(String)
    ez_wallet_id: Mapped[str | None] = mapped_column(String)
    # R1/R4: "unprovisioned" until workspace_client.lookup_or_login_or_create
    # succeeds; "active" once provisioned.
    workspace_account_status: Mapped[str] = mapped_column(
        String, default="unprovisioned"
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
