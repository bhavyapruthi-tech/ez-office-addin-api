import uuid
from typing import Literal

from pydantic import BaseModel, Field

Operation = Literal["flip", "translate", "both"]
Direction = Literal["rtl", "ltr"]
JobStatus = Literal["queued", "processing", "done", "partial_failed", "failed"]
ErrorCode = Literal[
    "insufficient_balance_after_spend",
    "workspace_unreachable",
    "stale_timeout",
    "debit_succeeded_status_write_failed",
]


class SubmitJobRequest(BaseModel):
    operation: Operation
    file_base64: str
    file_name: str
    lang_from: str | None = None
    lang_to: str | None = None
    direction: Direction | None = None
    expert_review: bool = False
    # KD3: UUID-formatted, length-capped -- rejected as 422 otherwise.
    idempotency_key: uuid.UUID = Field(...)


class SubmitJobResponse(BaseModel):
    job_id: str
    status: JobStatus


class PollJobResponse(BaseModel):
    status: JobStatus
    step: str | None
    result_file_base64: str | None
    credits_charged: int | None
    failed_operation: str | None
    error_code: ErrorCode | None
    localization_ref: str | None
