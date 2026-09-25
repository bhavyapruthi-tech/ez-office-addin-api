import uuid
from datetime import datetime

from sqlalchemy import (
    JSON,
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base

# R11/KD8: the four error_code values a `failed` job may carry.
ERROR_CODE_INSUFFICIENT_BALANCE_AFTER_SPEND = "insufficient_balance_after_spend"
ERROR_CODE_WORKSPACE_UNREACHABLE = "workspace_unreachable"
ERROR_CODE_STALE_TIMEOUT = "stale_timeout"
ERROR_CODE_DEBIT_SUCCEEDED_STATUS_WRITE_FAILED = "debit_succeeded_status_write_failed"


class ToolJob(Base):
    __tablename__ = "tool_jobs"
    __table_args__ = (
        UniqueConstraint("user_id", "idempotency_key", name="uq_tool_jobs_user_idem"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id"), index=True
    )
    operation: Mapped[str] = mapped_column(String)  # flip | translate | both
    status: Mapped[str] = mapped_column(
        String, default="queued", index=True
    )  # queued | processing | done | partial_failed | failed
    failed_operation: Mapped[str | None] = mapped_column(String)  # flip | translate
    error_code: Mapped[str | None] = mapped_column(String)

    # KD3: UUID-formatted, length-capped at the Pydantic schema layer (U5).
    idempotency_key: Mapped[str] = mapped_column(String)

    input_file_meta: Mapped[dict | None] = mapped_column(JSON)
    result_file_url: Mapped[str | None] = mapped_column(String)
    expert_review_requested: Mapped[bool] = mapped_column(default=False)
    # R19: persisted once generated so repeated polls return the same value.
    localization_ref: Mapped[str | None] = mapped_column(String)

    credits_cost: Mapped[int | None] = mapped_column(Integer)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
