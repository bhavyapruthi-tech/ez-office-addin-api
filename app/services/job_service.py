import asyncio
import logging
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import settings
from app.models.tool_job import (
    ERROR_CODE_DEBIT_SUCCEEDED_STATUS_WRITE_FAILED,
    ERROR_CODE_INSUFFICIENT_BALANCE_AFTER_SPEND,
    ERROR_CODE_STALE_TIMEOUT,
    ERROR_CODE_WORKSPACE_UNREACHABLE,
    ToolJob,
)
from app.models.user import User
from app.services.flip_client import flip_client
from app.services.ref_number import generate_ref
from app.services.translate_client import translate_client
from app.services.workspace_client import WorkspaceClient

logger = logging.getLogger(__name__)

# Placeholder cost formula -- ARCHITECTURE.md sec10 flags the real formula
# as "pending upstream docs, not blocking." These mirror the spec's own
# example numbers (Section 02: "Cost: 12 cr.", "~8 cr./1,000 words").
FLIP_COST_CREDITS = 12
TRANSLATE_COST_PER_1000_WORDS = 8
DEFAULT_WORD_COUNT_ESTIMATE = 1000


def estimate_credits_cost(operation: str, word_count: int | None = None) -> int:
    cost = 0
    if operation in ("flip", "both"):
        cost += FLIP_COST_CREDITS
    if operation in ("translate", "both"):
        words = word_count or DEFAULT_WORD_COUNT_ESTIMATE
        cost += round(words / 1000 * TRANSLATE_COST_PER_1000_WORDS)
    return cost


class JobRunner(Protocol):
    def submit(self, job: ToolJob, file_base64: str) -> None: ...


class InProcessJobRunner:
    """KTD1: in-process BackgroundTasks, no worker queue in v1."""

    def __init__(
        self,
        background_tasks: Any,
        sessionmaker: async_sessionmaker[AsyncSession],
        workspace_client: WorkspaceClient,
    ):
        self._background_tasks = background_tasks
        self._sessionmaker = sessionmaker
        self._workspace_client = workspace_client

    def submit(self, job: ToolJob, file_base64: str) -> None:
        self._background_tasks.add_task(
            _run, job.id, file_base64, self._sessionmaker, self._workspace_client
        )


async def get_or_create_job(
    db: AsyncSession,
    *,
    user_id: Any,
    operation: str,
    idempotency_key: str,
    input_file_meta: dict[str, Any],
    expert_review_requested: bool,
) -> tuple[ToolJob, bool]:
    """R8/KD3: two-phase dedup. A fast-path SELECT catches the common
    repeat-submit case with no balance check and no Workspace call. The
    optimistic-insert-and-catch below is what actually guarantees
    correctness under concurrency (a double-click, or a genuine client
    retry racing the original) -- the SELECT is a fast path, not the
    safety mechanism. Returns (job, created).
    """
    existing = await db.execute(
        select(ToolJob).where(
            ToolJob.user_id == user_id, ToolJob.idempotency_key == idempotency_key
        )
    )
    job = existing.scalar_one_or_none()
    if job is not None:
        return job, False

    job = ToolJob(
        user_id=user_id,
        operation=operation,
        idempotency_key=idempotency_key,
        input_file_meta=input_file_meta,
        expert_review_requested=expert_review_requested,
        status="queued",
    )
    db.add(job)
    try:
        await db.flush()
    except IntegrityError:
        await db.rollback()
        existing = await db.execute(
            select(ToolJob).where(
                ToolJob.user_id == user_id, ToolJob.idempotency_key == idempotency_key
            )
        )
        job = existing.scalar_one()
        return job, False

    return job, True


async def _run(
    job_id: Any,
    file_base64: str,
    sessionmaker: async_sessionmaker[AsyncSession],
    workspace_client: WorkspaceClient,
) -> None:
    # Phase 1: read job + owning user's wallet id. Session closes before the
    # (slow) upstream calls below so it's never held open across them.
    async with sessionmaker() as db:
        job = await db.get(ToolJob, job_id)
        if job is None:
            return
        operation = job.operation
        meta = job.input_file_meta or {}
        direction = meta.get("direction", "rtl")
        lang_from = meta.get("lang_from")
        lang_to = meta.get("lang_to")

        user = await db.get(User, job.user_id)
        wallet_id = user.ez_wallet_id if user else None

    # Phase 2: call upstream (no open DB session held across this).
    flip_result: dict[str, Any] | None = None
    translate_result: dict[str, Any] | None = None
    flip_failed = False
    translate_failed = False

    if operation in ("flip", "both"):
        try:
            flip_result = await flip_client.flip(file_base64, direction)
            file_base64 = flip_result.get("file_base64", file_base64)
        except Exception:
            logger.exception("flip upstream call failed for job %s", job_id)
            flip_failed = True

    if operation in ("translate", "both"):
        try:
            translate_result = await translate_client.translate(
                file_base64, lang_from or "en", lang_to or "ar"
            )
        except Exception:
            logger.exception("translate upstream call failed for job %s", job_id)
            translate_failed = True

    both_failed = operation == "both" and flip_failed and translate_failed
    single_op_failed = (operation == "flip" and flip_failed) or (
        operation == "translate" and translate_failed
    )

    # Phase 3: reconcile in a fresh session.
    async with sessionmaker() as db:
        job = await db.get(ToolJob, job_id)
        if job is None:
            return

        # KD6: re-read status before touching Workspace -- the recurring
        # sweep may have already closed this row out while the (slow)
        # upstream calls above were in flight. Whichever writer reaches
        # "processing -> terminal" first wins; the loser never debits.
        if job.status not in ("queued", "processing"):
            return

        if both_failed or single_op_failed:
            job.status = "failed"
            job.error_code = ERROR_CODE_WORKSPACE_UNREACHABLE
            job.completed_at = datetime.now(UTC)
            await db.commit()
            return

        word_count = translate_result.get("word_count") if translate_result else None

        if operation == "both" and (flip_failed != translate_failed):
            pending_status = "partial_failed"
            failed_operation = "flip" if flip_failed else "translate"
            succeeded_operation = "translate" if flip_failed else "flip"
            cost = estimate_credits_cost(succeeded_operation, word_count)
            if flip_failed:
                result_base64 = translate_result.get("file_base64")  # type: ignore[union-attr]
            else:
                result_base64 = flip_result.get("file_base64")  # type: ignore[union-attr]
        else:
            pending_status = "done"
            failed_operation = None
            cost = estimate_credits_cost(operation, word_count)
            result_base64 = (
                translate_result.get("file_base64")
                if translate_result
                else (flip_result.get("file_base64") if flip_result else None)
            )

        debit_result: dict[str, Any] = {"status": "workspace_unreachable"}
        try:
            if wallet_id:
                debit_result = await workspace_client.debit(
                    wallet_id, cost, idempotency_key=str(job.id), reason="job"
                )
        except Exception:
            logger.exception("debit call failed for job %s", job_id)
            debit_result = {"status": "workspace_unreachable"}

        if debit_result.get("status") == "ok":
            job.status = pending_status
            job.failed_operation = failed_operation
            job.credits_cost = cost
            job.result_file_url = result_base64
            job.completed_at = datetime.now(UTC)
            if job.expert_review_requested and pending_status in (
                "done",
                "partial_failed",
            ):
                job.localization_ref = generate_ref()
        elif debit_result.get("status") == "insufficient_balance":
            job.status = "failed"
            job.error_code = ERROR_CODE_INSUFFICIENT_BALANCE_AFTER_SPEND
            job.completed_at = datetime.now(UTC)
        else:
            job.status = "failed"
            job.error_code = ERROR_CODE_WORKSPACE_UNREACHABLE
            job.completed_at = datetime.now(UTC)

        await db.commit()


async def run_stale_job_sweep(
    sessionmaker: async_sessionmaker[AsyncSession], workspace_client: WorkspaceClient
) -> None:
    """KD6: marks aged queued/processing rows failed. Before concluding a
    row was never debited, queries Workspace for a matching debit -- a
    match means the debit actually succeeded and only the local status
    write failed (KD8), which must never be treated as safe-to-retry.
    """
    cutoff = datetime.now(UTC) - timedelta(
        seconds=settings.poll_timeout_ceiling_seconds
    )
    async with sessionmaker() as db:
        result = await db.execute(
            select(ToolJob).where(
                ToolJob.status.in_(["queued", "processing"]),
                ToolJob.created_at < cutoff,
            )
        )
        stale_jobs = result.scalars().all()

        for job in stale_jobs:
            user = await db.get(User, job.user_id)
            wallet_id = user.ez_wallet_id if user else None
            matching_debit = None
            if wallet_id:
                try:
                    matching_debit = (
                        await workspace_client.find_debit_by_idempotency_key(
                            wallet_id, str(job.id)
                        )
                    )
                except Exception:
                    logger.exception("sweep: debit lookup failed for job %s", job.id)

            job.status = "failed"
            job.error_code = (
                ERROR_CODE_DEBIT_SUCCEEDED_STATUS_WRITE_FAILED
                if matching_debit is not None
                else ERROR_CODE_STALE_TIMEOUT
            )
            job.completed_at = datetime.now(UTC)

        await db.commit()


async def sweep_loop(
    sessionmaker: async_sessionmaker[AsyncSession], workspace_client: WorkspaceClient
) -> None:
    while True:
        await asyncio.sleep(settings.sweep_interval_seconds)
        try:
            await run_stale_job_sweep(sessionmaker, workspace_client)
        except Exception:
            logger.exception("stale job sweep failed")
