from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.models.tool_job import ToolJob
from app.models.user import User
from app.services import job_service


async def _make_user(db_session, *, wallet_id="wallet-1", status="active") -> User:
    user = User(
        work_email="a@example.com",
        ms_oid="oid-1",
        ez_wallet_id=wallet_id,
        workspace_account_status=status,
    )
    db_session.add(user)
    await db_session.commit()
    return user


async def _reload(db_engine, job_id) -> ToolJob:
    """A fresh session per read. aiosqlite's single shared connection
    (StaticPool) does not tolerate a session object being reused after a
    *different* session has driven it in between -- always open a new one.
    """
    async with async_sessionmaker(db_engine, expire_on_commit=False)() as s:
        return await s.get(ToolJob, job_id)


@pytest.mark.anyio
async def test_get_or_create_job_dedups_on_repeat_key(db_session):
    user = await _make_user(db_session)
    job1, created1 = await job_service.get_or_create_job(
        db_session,
        user_id=user.id,
        operation="flip",
        idempotency_key="key-1",
        input_file_meta={"file_name": "a.pptx", "direction": "rtl"},
        expert_review_requested=False,
    )
    await db_session.commit()
    job2, created2 = await job_service.get_or_create_job(
        db_session,
        user_id=user.id,
        operation="flip",
        idempotency_key="key-1",
        input_file_meta={"file_name": "a.pptx", "direction": "rtl"},
        expert_review_requested=False,
    )
    assert created1 is True
    assert created2 is False
    assert job1.id == job2.id


@pytest.mark.anyio
async def test_get_or_create_job_concurrent_submits_yield_one_row(
    db_session, db_engine, monkeypatch
):
    """R8/KD3: a concurrent duplicate that races past the fast-path SELECT
    is still caught by the insert-time unique-violation catch."""
    user = await _make_user(db_session)

    original_flush = db_session.flush
    call_count = 0

    async def racing_flush(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            # Simulate a second request's row landing first, from a
            # separate session sharing the same in-memory sqlite DB (via
            # the shared `db_engine`), before this flush commits.
            async with async_sessionmaker(db_engine, expire_on_commit=False)() as other:
                other.add(
                    ToolJob(
                        user_id=user.id,
                        operation="flip",
                        idempotency_key="race-key",
                        input_file_meta={},
                        status="queued",
                    )
                )
                await other.commit()
        return await original_flush(*args, **kwargs)

    monkeypatch.setattr(db_session, "flush", racing_flush)

    job, created = await job_service.get_or_create_job(
        db_session,
        user_id=user.id,
        operation="flip",
        idempotency_key="race-key",
        input_file_meta={},
        expert_review_requested=False,
    )
    assert created is False
    await db_session.commit()

    async with async_sessionmaker(db_engine, expire_on_commit=False)() as check:
        result = await check.execute(
            select(ToolJob).where(ToolJob.idempotency_key == "race-key")
        )
        assert len(result.scalars().all()) == 1


@pytest.mark.anyio
async def test_get_or_create_job_sets_step_received_on_creation(db_session):
    user = await _make_user(db_session)
    job, created = await job_service.get_or_create_job(
        db_session,
        user_id=user.id,
        operation="flip",
        idempotency_key="key-step",
        input_file_meta={},
        expert_review_requested=False,
    )
    assert created is True
    assert job.step == "received"


@pytest.mark.anyio
async def test_run_flip_writes_applying_rtl_step_before_calling_flip(
    db_session, db_engine, monkeypatch
):
    """Code-review finding #9: step must actually reach the DB before the
    upstream call starts, not just get set on the in-memory object -- read
    it back from a separate session while flip is "in flight" to prove the
    write already landed."""
    user = await _make_user(db_session)
    job = ToolJob(
        user_id=user.id,
        operation="flip",
        idempotency_key="k-step-flip",
        input_file_meta={"direction": "rtl"},
        status="queued",
    )
    db_session.add(job)
    await db_session.commit()
    job_id = job.id

    observed_step = None

    async def observing_flip(*args, **kwargs):
        nonlocal observed_step
        observed_step = (await _reload(db_engine, job_id)).step
        return {"file_base64": "x", "status": "done"}

    monkeypatch.setattr(job_service.flip_client, "flip", observing_flip)
    workspace_client = AsyncMock()
    workspace_client.debit.return_value = {"status": "ok"}
    sessionmaker = async_sessionmaker(db_engine, expire_on_commit=False)

    await job_service._run(job_id, "orig", sessionmaker, workspace_client)

    assert observed_step == "applying_rtl"


@pytest.mark.anyio
async def test_run_translate_only_never_writes_applying_rtl_step(
    db_session, db_engine, monkeypatch
):
    user = await _make_user(db_session)
    job = ToolJob(
        user_id=user.id,
        operation="translate",
        idempotency_key="k-step-translate",
        input_file_meta={"lang_from": "en", "lang_to": "ar"},
        status="queued",
    )
    db_session.add(job)
    await db_session.commit()
    job_id = job.id

    observed_step = None

    async def observing_translate(*args, **kwargs):
        nonlocal observed_step
        observed_step = (await _reload(db_engine, job_id)).step
        return {"file_base64": "x", "status": "done", "word_count": 500}

    monkeypatch.setattr(job_service.translate_client, "translate", observing_translate)
    workspace_client = AsyncMock()
    workspace_client.debit.return_value = {"status": "ok"}
    sessionmaker = async_sessionmaker(db_engine, expire_on_commit=False)

    await job_service._run(job_id, "orig", sessionmaker, workspace_client)

    assert observed_step == "translating"


@pytest.mark.anyio
async def test_run_both_operation_writes_steps_in_actual_execution_order(
    db_session, db_engine, monkeypatch
):
    """The real backend calls flip before translate for "both" -- the
    mock-era frontend's step order assumed the opposite. Now that the
    backend's own written step is authoritative, prove it reflects the
    real order: applying_rtl while flip runs, translating while translate
    runs, in that sequence."""
    user = await _make_user(db_session)
    job = ToolJob(
        user_id=user.id,
        operation="both",
        idempotency_key="k-step-both",
        input_file_meta={"direction": "rtl", "lang_from": "en", "lang_to": "ar"},
        status="queued",
    )
    db_session.add(job)
    await db_session.commit()
    job_id = job.id

    observed_steps: list[str] = []

    async def observing_flip(*args, **kwargs):
        observed_steps.append((await _reload(db_engine, job_id)).step)
        return {"file_base64": "flipped", "status": "done"}

    async def observing_translate(*args, **kwargs):
        observed_steps.append((await _reload(db_engine, job_id)).step)
        return {"file_base64": "translated", "status": "done", "word_count": 500}

    monkeypatch.setattr(job_service.flip_client, "flip", observing_flip)
    monkeypatch.setattr(job_service.translate_client, "translate", observing_translate)
    workspace_client = AsyncMock()
    workspace_client.debit.return_value = {"status": "ok"}
    sessionmaker = async_sessionmaker(db_engine, expire_on_commit=False)

    await job_service._run(job_id, "orig", sessionmaker, workspace_client)

    assert observed_steps == ["applying_rtl", "translating"]


@pytest.mark.anyio
async def test_run_writes_quality_check_step_before_debit_call(
    db_session, db_engine, monkeypatch
):
    user = await _make_user(db_session)
    job = ToolJob(
        user_id=user.id,
        operation="flip",
        idempotency_key="k-step-quality",
        input_file_meta={"direction": "rtl"},
        status="queued",
    )
    db_session.add(job)
    await db_session.commit()
    job_id = job.id

    monkeypatch.setattr(
        job_service.flip_client,
        "flip",
        AsyncMock(return_value={"file_base64": "x", "status": "done"}),
    )
    observed_step = None

    workspace_client = AsyncMock()

    async def observing_debit(*args, **kwargs):
        nonlocal observed_step
        observed_step = (await _reload(db_engine, job_id)).step
        return {"status": "ok"}

    workspace_client.debit.side_effect = observing_debit
    sessionmaker = async_sessionmaker(db_engine, expire_on_commit=False)

    await job_service._run(job_id, "orig", sessionmaker, workspace_client)

    assert observed_step == "quality_check"


@pytest.mark.anyio
async def test_run_upstream_failure_never_reaches_quality_check_step(
    db_session, db_engine, monkeypatch
):
    """The quality_check step implies upstream succeeded -- a job that
    never gets there (single-op upstream failure) must not claim it did."""
    user = await _make_user(db_session)
    job = ToolJob(
        user_id=user.id,
        operation="flip",
        idempotency_key="k-step-fail",
        input_file_meta={"direction": "rtl"},
        status="queued",
    )
    db_session.add(job)
    await db_session.commit()
    job_id = job.id

    monkeypatch.setattr(
        job_service.flip_client,
        "flip",
        AsyncMock(side_effect=RuntimeError("upstream down")),
    )
    workspace_client = AsyncMock()
    sessionmaker = async_sessionmaker(db_engine, expire_on_commit=False)

    await job_service._run(job_id, "orig", sessionmaker, workspace_client)

    reloaded = await _reload(db_engine, job_id)
    assert reloaded.status == "failed"
    assert reloaded.step == "applying_rtl"  # last real step reached, untouched


@pytest.mark.anyio
async def test_run_happy_path_flip_computes_cost_and_marks_done(
    db_session, db_engine, monkeypatch
):
    user = await _make_user(db_session)
    job = ToolJob(
        user_id=user.id,
        operation="flip",
        idempotency_key="k1",
        input_file_meta={"direction": "rtl"},
        status="queued",
    )
    db_session.add(job)
    await db_session.commit()
    job_id = job.id

    monkeypatch.setattr(
        job_service.flip_client,
        "flip",
        AsyncMock(return_value={"file_base64": "flipped-content", "status": "done"}),
    )
    workspace_client = AsyncMock()
    workspace_client.debit.return_value = {"credit_balance": 88, "status": "ok"}

    sessionmaker = async_sessionmaker(db_engine, expire_on_commit=False)
    await job_service._run(job_id, "original-content", sessionmaker, workspace_client)

    reloaded = await _reload(db_engine, job_id)
    assert reloaded.status == "done"
    assert reloaded.credits_cost == job_service.FLIP_COST_CREDITS
    assert reloaded.result_file_url == "flipped-content"
    workspace_client.debit.assert_awaited_once()


@pytest.mark.anyio
async def test_run_expert_review_populates_localization_ref(
    db_session, db_engine, monkeypatch
):
    user = await _make_user(db_session)
    job = ToolJob(
        user_id=user.id,
        operation="flip",
        idempotency_key="k-expert",
        input_file_meta={"direction": "rtl"},
        expert_review_requested=True,
        status="queued",
    )
    db_session.add(job)
    await db_session.commit()
    job_id = job.id

    monkeypatch.setattr(
        job_service.flip_client,
        "flip",
        AsyncMock(return_value={"file_base64": "x", "status": "done"}),
    )
    workspace_client = AsyncMock()
    workspace_client.debit.return_value = {"status": "ok"}
    sessionmaker = async_sessionmaker(db_engine, expire_on_commit=False)

    await job_service._run(job_id, "orig", sessionmaker, workspace_client)
    first_ref = (await _reload(db_engine, job_id)).localization_ref
    assert first_ref is not None

    # A second read (simulating a repeated poll) must return the same,
    # already-persisted value -- R19 writes it once at completion.
    second_ref = (await _reload(db_engine, job_id)).localization_ref
    assert second_ref == first_ref


@pytest.mark.anyio
async def test_run_single_op_upstream_failure_marks_workspace_unreachable_no_debit(
    db_session, db_engine, monkeypatch
):
    """P0 #1: when the sole upstream call for a single-operation job raises,
    the job must be marked failed/workspace_unreachable and never reach the
    debit call -- not silently fall through into the done-completion path."""
    user = await _make_user(db_session)
    job = ToolJob(
        user_id=user.id,
        operation="flip",
        idempotency_key="k-flip-raises",
        input_file_meta={"direction": "rtl"},
        status="queued",
    )
    db_session.add(job)
    await db_session.commit()
    job_id = job.id

    monkeypatch.setattr(
        job_service.flip_client,
        "flip",
        AsyncMock(side_effect=RuntimeError("upstream down")),
    )
    workspace_client = AsyncMock()
    sessionmaker = async_sessionmaker(db_engine, expire_on_commit=False)

    await job_service._run(job_id, "orig", sessionmaker, workspace_client)

    reloaded = await _reload(db_engine, job_id)
    assert reloaded.status == "failed"
    assert reloaded.error_code == "workspace_unreachable"
    workspace_client.debit.assert_not_awaited()


@pytest.mark.anyio
async def test_run_both_operation_both_upstream_calls_fail_marks_workspace_unreachable(
    db_session, db_engine, monkeypatch
):
    """P0 #1: when BOTH upstream calls for a "both" job raise, the job must
    be marked failed/workspace_unreachable and never reach the debit call."""
    user = await _make_user(db_session)
    job = ToolJob(
        user_id=user.id,
        operation="both",
        idempotency_key="k-both-raise",
        input_file_meta={"direction": "rtl", "lang_from": "en", "lang_to": "ar"},
        status="queued",
    )
    db_session.add(job)
    await db_session.commit()
    job_id = job.id

    monkeypatch.setattr(
        job_service.flip_client,
        "flip",
        AsyncMock(side_effect=RuntimeError("flip upstream down")),
    )
    monkeypatch.setattr(
        job_service.translate_client,
        "translate",
        AsyncMock(side_effect=RuntimeError("translate upstream down")),
    )
    workspace_client = AsyncMock()
    sessionmaker = async_sessionmaker(db_engine, expire_on_commit=False)

    await job_service._run(job_id, "orig", sessionmaker, workspace_client)

    reloaded = await _reload(db_engine, job_id)
    assert reloaded.status == "failed"
    assert reloaded.error_code == "workspace_unreachable"
    workspace_client.debit.assert_not_awaited()


@pytest.mark.anyio
async def test_run_both_operation_translate_fails_marks_partial_failed(
    db_session, db_engine, monkeypatch
):
    user = await _make_user(db_session)
    job = ToolJob(
        user_id=user.id,
        operation="both",
        idempotency_key="k-both",
        input_file_meta={"direction": "rtl", "lang_from": "en", "lang_to": "ar"},
        status="queued",
    )
    db_session.add(job)
    await db_session.commit()
    job_id = job.id

    monkeypatch.setattr(
        job_service.flip_client,
        "flip",
        AsyncMock(return_value={"file_base64": "flipped-only", "status": "done"}),
    )
    monkeypatch.setattr(
        job_service.translate_client,
        "translate",
        AsyncMock(side_effect=RuntimeError("upstream down")),
    )
    workspace_client = AsyncMock()
    workspace_client.debit.return_value = {"status": "ok"}
    sessionmaker = async_sessionmaker(db_engine, expire_on_commit=False)

    await job_service._run(job_id, "orig", sessionmaker, workspace_client)

    reloaded = await _reload(db_engine, job_id)
    assert reloaded.status == "partial_failed"
    assert reloaded.failed_operation == "translate"
    assert reloaded.credits_cost == job_service.FLIP_COST_CREDITS
    assert reloaded.result_file_url == "flipped-only"
    workspace_client.debit.assert_awaited_once_with(
        "wallet-1",
        job_service.FLIP_COST_CREDITS,
        idempotency_key=str(job_id),
        reason="job",
    )


@pytest.mark.anyio
async def test_run_both_operation_flip_fails_marks_partial_failed(
    db_session, db_engine, monkeypatch
):
    """Mirror of test_run_both_operation_translate_fails_marks_partial_failed:
    flip raises, translate succeeds. Exercises the `if flip_failed:` branch
    of the result/cost selector (P1 #4) -- the previously-untested arm."""
    user = await _make_user(db_session)
    job = ToolJob(
        user_id=user.id,
        operation="both",
        idempotency_key="k-both-flip-fails",
        input_file_meta={"direction": "rtl", "lang_from": "en", "lang_to": "ar"},
        status="queued",
    )
    db_session.add(job)
    await db_session.commit()
    job_id = job.id

    monkeypatch.setattr(
        job_service.flip_client,
        "flip",
        AsyncMock(side_effect=RuntimeError("flip upstream down")),
    )
    monkeypatch.setattr(
        job_service.translate_client,
        "translate",
        AsyncMock(
            return_value={
                "file_base64": "translated-only",
                "status": "done",
                "word_count": 2000,
            }
        ),
    )
    workspace_client = AsyncMock()
    workspace_client.debit.return_value = {"status": "ok"}
    sessionmaker = async_sessionmaker(db_engine, expire_on_commit=False)

    await job_service._run(job_id, "orig", sessionmaker, workspace_client)

    expected_cost = job_service.estimate_credits_cost("translate", 2000)
    reloaded = await _reload(db_engine, job_id)
    assert reloaded.status == "partial_failed"
    assert reloaded.failed_operation == "flip"
    assert reloaded.credits_cost == expected_cost
    assert reloaded.result_file_url == "translated-only"
    workspace_client.debit.assert_awaited_once_with(
        "wallet-1",
        expected_cost,
        idempotency_key=str(job_id),
        reason="job",
    )


@pytest.mark.anyio
async def test_run_debit_insufficient_balance_after_spend(
    db_session, db_engine, monkeypatch
):
    user = await _make_user(db_session)
    job = ToolJob(
        user_id=user.id,
        operation="flip",
        idempotency_key="k-insuff",
        input_file_meta={"direction": "rtl"},
        status="queued",
    )
    db_session.add(job)
    await db_session.commit()
    job_id = job.id

    monkeypatch.setattr(
        job_service.flip_client,
        "flip",
        AsyncMock(return_value={"file_base64": "x", "status": "done"}),
    )
    workspace_client = AsyncMock()
    workspace_client.debit.return_value = {"status": "insufficient_balance"}
    sessionmaker = async_sessionmaker(db_engine, expire_on_commit=False)

    await job_service._run(job_id, "orig", sessionmaker, workspace_client)

    reloaded = await _reload(db_engine, job_id)
    assert reloaded.status == "failed"
    assert reloaded.error_code == "insufficient_balance_after_spend"
    assert reloaded.result_file_url is None


@pytest.mark.anyio
async def test_run_debit_timeout_marks_workspace_unreachable_resubmit_safe(
    db_session, db_engine, monkeypatch
):
    user = await _make_user(db_session)
    job = ToolJob(
        user_id=user.id,
        operation="flip",
        idempotency_key="k-timeout",
        input_file_meta={"direction": "rtl"},
        status="queued",
    )
    db_session.add(job)
    await db_session.commit()
    job_id = job.id

    monkeypatch.setattr(
        job_service.flip_client,
        "flip",
        AsyncMock(return_value={"file_base64": "x", "status": "done"}),
    )
    workspace_client = AsyncMock()
    workspace_client.debit.side_effect = TimeoutError()
    sessionmaker = async_sessionmaker(db_engine, expire_on_commit=False)

    await job_service._run(job_id, "orig", sessionmaker, workspace_client)

    reloaded = await _reload(db_engine, job_id)
    assert reloaded.status == "failed"
    assert reloaded.error_code == "workspace_unreachable"

    # A client retry on the same job is a fresh POST /tools/jobs with the
    # SAME idempotency_key, which get_or_create_job returns unchanged.
    async with async_sessionmaker(db_engine, expire_on_commit=False)() as retry_db:
        _job2, created2 = await job_service.get_or_create_job(
            retry_db,
            user_id=user.id,
            operation="flip",
            idempotency_key="k-timeout",
            input_file_meta={},
            expert_review_requested=False,
        )
        assert created2 is False


@pytest.mark.anyio
async def test_run_no_wallet_id_skips_debit_marks_workspace_unreachable(
    db_session, db_engine, monkeypatch
):
    """P1 #5: when the owning user has no ez_wallet_id, the `if wallet_id:`
    guard must skip the debit call entirely rather than call it with a
    falsy wallet id, and the job must still resolve to a terminal failure."""
    user = await _make_user(db_session, wallet_id=None)
    job = ToolJob(
        user_id=user.id,
        operation="flip",
        idempotency_key="k-no-wallet",
        input_file_meta={"direction": "rtl"},
        status="queued",
    )
    db_session.add(job)
    await db_session.commit()
    job_id = job.id

    monkeypatch.setattr(
        job_service.flip_client,
        "flip",
        AsyncMock(return_value={"file_base64": "x", "status": "done"}),
    )
    workspace_client = AsyncMock()
    sessionmaker = async_sessionmaker(db_engine, expire_on_commit=False)

    await job_service._run(job_id, "orig", sessionmaker, workspace_client)

    workspace_client.debit.assert_not_awaited()
    reloaded = await _reload(db_engine, job_id)
    assert reloaded.status == "failed"
    assert reloaded.error_code == "workspace_unreachable"


@pytest.mark.anyio
async def test_run_aborts_without_debit_if_sweep_already_closed_row(
    db_session, db_engine, monkeypatch
):
    """KD6: the recurring sweep and an in-flight _run task must be mutually
    exclusive within one process -- whichever reaches processing->terminal
    first wins, and the loser never debits."""
    user = await _make_user(db_session)
    job = ToolJob(
        user_id=user.id,
        operation="flip",
        idempotency_key="k-race",
        input_file_meta={"direction": "rtl"},
        status="queued",
    )
    db_session.add(job)
    await db_session.commit()
    job_id = job.id

    async def slow_flip(*args, **kwargs):
        # While this "upstream call" is in flight, the sweep closes the row.
        async with async_sessionmaker(db_engine, expire_on_commit=False)() as sweep_db:
            sweep_job = await sweep_db.get(ToolJob, job_id)
            sweep_job.status = "failed"
            sweep_job.error_code = "stale_timeout"
            await sweep_db.commit()
        return {"file_base64": "x", "status": "done"}

    monkeypatch.setattr(job_service.flip_client, "flip", slow_flip)
    workspace_client = AsyncMock()
    sessionmaker = async_sessionmaker(db_engine, expire_on_commit=False)

    await job_service._run(job_id, "orig", sessionmaker, workspace_client)

    workspace_client.debit.assert_not_awaited()
    reloaded = await _reload(db_engine, job_id)
    assert reloaded.status == "failed"
    assert reloaded.error_code == "stale_timeout"  # untouched by _run


@pytest.mark.anyio
async def test_sweep_marks_stale_row_with_no_matching_debit_as_stale_timeout(
    db_session, db_engine
):
    user = await _make_user(db_session)
    old_job = ToolJob(
        user_id=user.id,
        operation="flip",
        idempotency_key="k-stale",
        input_file_meta={},
        status="processing",
        created_at=datetime.now(UTC) - timedelta(hours=1),
    )
    db_session.add(old_job)
    await db_session.commit()
    job_id = old_job.id

    workspace_client = AsyncMock()
    workspace_client.find_debit_by_idempotency_key.return_value = None
    sessionmaker = async_sessionmaker(db_engine, expire_on_commit=False)

    await job_service.run_stale_job_sweep(sessionmaker, workspace_client)

    reloaded = await _reload(db_engine, job_id)
    assert reloaded.status == "failed"
    assert reloaded.error_code == "stale_timeout"


@pytest.mark.anyio
async def test_sweep_marks_stale_row_with_matching_debit_as_status_write_failed(
    db_session, db_engine
):
    user = await _make_user(db_session)
    old_job = ToolJob(
        user_id=user.id,
        operation="flip",
        idempotency_key="k-charged",
        input_file_meta={},
        status="processing",
        created_at=datetime.now(UTC) - timedelta(hours=1),
    )
    db_session.add(old_job)
    await db_session.commit()
    job_id = old_job.id

    workspace_client = AsyncMock()
    workspace_client.find_debit_by_idempotency_key.return_value = {
        "credit_balance": 50,
        "status": "ok",
    }
    sessionmaker = async_sessionmaker(db_engine, expire_on_commit=False)

    await job_service.run_stale_job_sweep(sessionmaker, workspace_client)

    reloaded = await _reload(db_engine, job_id)
    assert reloaded.status == "failed"
    assert reloaded.error_code == "debit_succeeded_status_write_failed"


@pytest.mark.anyio
async def test_sweep_does_not_clobber_row_completed_done_during_lookup_gather(
    db_session, db_engine, monkeypatch
):
    """P0 #2, reverse-race direction of
    test_run_aborts_without_debit_if_sweep_already_closed_row: the sweep
    snapshots a stale row via its initial SELECT, then -- while its
    concurrent Workspace lookups (asyncio.gather) are in flight -- a
    same-row _run task reaches its own Phase 3 re-check, finds the row
    still processing, successfully debits, and commits STATUS_DONE. The
    sweep's final write must re-check current status and must NOT overwrite
    that terminal DONE outcome back to failed/stale_timeout."""
    user = await _make_user(db_session)
    old_job = ToolJob(
        user_id=user.id,
        operation="flip",
        idempotency_key="k-reverse-race",
        input_file_meta={"direction": "rtl"},
        status="processing",
        created_at=datetime.now(UTC) - timedelta(hours=1),
    )
    db_session.add(old_job)
    await db_session.commit()
    job_id = old_job.id

    async def slow_lookup(*args, **kwargs):
        # While the sweep's Workspace round-trip is in flight, a concurrent
        # _run task for this same row reaches Phase 3, debits, and commits
        # STATUS_DONE.
        monkeypatch.setattr(
            job_service.flip_client,
            "flip",
            AsyncMock(return_value={"file_base64": "flipped", "status": "done"}),
        )
        run_workspace_client = AsyncMock()
        run_workspace_client.debit.return_value = {"status": "ok"}
        run_sessionmaker = async_sessionmaker(db_engine, expire_on_commit=False)
        await job_service._run(job_id, "orig", run_sessionmaker, run_workspace_client)
        return None

    workspace_client = AsyncMock()
    workspace_client.find_debit_by_idempotency_key.side_effect = slow_lookup
    sessionmaker = async_sessionmaker(db_engine, expire_on_commit=False)

    await job_service.run_stale_job_sweep(sessionmaker, workspace_client)

    reloaded = await _reload(db_engine, job_id)
    assert reloaded.status == "done"  # untouched by the sweep's final write
    assert reloaded.result_file_url == "flipped"
