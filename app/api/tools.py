from typing import Any
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_session, get_current_session_allow_expired, get_db
from app.core.errors import (
    InsufficientBalanceError,
    NotFoundError,
    WorkspaceUnprovisionedError,
)
from app.models.tool_job import ToolJob
from app.models.user import User
from app.schemas.tools import PollJobResponse, SubmitJobRequest, SubmitJobResponse
from app.services import job_service
from app.services.workspace_client import workspace_client

router = APIRouter()


@router.post("/tools/jobs", response_model=SubmitJobResponse)
async def submit_job(
    body: SubmitJobRequest,
    background_tasks: BackgroundTasks,
    request: Request,
    user: User = Depends(get_current_session),
    db: AsyncSession = Depends(get_db),
) -> SubmitJobResponse:
    # R4/KD4: unprovisioned short-circuits before the idempotency lookup or
    # balance check.
    if user.workspace_account_status != "active" or not user.ez_wallet_id:
        raise WorkspaceUnprovisionedError()

    input_file_meta: dict[str, Any] = {
        "file_name": body.file_name,
        "direction": body.direction,
        "lang_from": body.lang_from,
        "lang_to": body.lang_to,
    }

    job, created = await job_service.get_or_create_job(
        db,
        user_id=user.id,
        operation=body.operation,
        idempotency_key=str(body.idempotency_key),
        input_file_meta=input_file_meta,
        expert_review_requested=body.expert_review,
    )

    if not created:
        await db.commit()
        return SubmitJobResponse(job_id=str(job.id), status=job.status)  # type: ignore[arg-type]

    # R9: pre-flight balance check, after dedup, before any upstream call.
    # Uses the same formula as final billing (job_service.estimate_credits_cost)
    # so the estimate and the actual charge never silently diverge.
    estimate = job_service.estimate_credits_cost(body.operation)
    balance = await workspace_client.get_balance(user.ez_wallet_id)
    if balance < estimate:
        await db.rollback()
        raise InsufficientBalanceError()

    await db.commit()

    runner = job_service.InProcessJobRunner(
        background_tasks, request.app.state.sessionmaker, workspace_client
    )
    runner.submit(job, body.file_base64)

    return SubmitJobResponse(job_id=str(job.id), status=job.status)  # type: ignore[arg-type]


@router.get("/tools/jobs/{job_id}", response_model=PollJobResponse)
async def get_job(
    job_id: str,
    user: User = Depends(get_current_session_allow_expired),
    db: AsyncSession = Depends(get_db),
) -> PollJobResponse:
    job = await db.get(ToolJob, UUID(job_id))
    if job is None or job.user_id != user.id:
        raise NotFoundError()

    # Code-review finding #9: the real, persisted step value, not a
    # hardcoded placeholder -- see job_service._run's phase transitions.
    step = job.step if job.status in ("queued", "processing") else None

    return PollJobResponse(
        status=job.status,  # type: ignore[arg-type]
        step=step,
        result_file_base64=job.result_file_url,
        credits_charged=job.credits_cost,
        failed_operation=job.failed_operation,
        error_code=job.error_code,  # type: ignore[arg-type]
        localization_ref=job.localization_ref,
    )
