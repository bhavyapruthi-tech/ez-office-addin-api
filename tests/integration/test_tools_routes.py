import hashlib
import secrets
import uuid
from datetime import UTC, datetime, timedelta

import pytest
import respx
from conftest import override_get_db
from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.api.deps import get_db
from app.main import create_app
from app.models.session import Session
from app.models.tool_job import ToolJob
from app.models.user import User


@pytest.fixture
async def app_and_token(db_session, db_engine):
    user = User(
        work_email="a@example.com",
        ms_oid="oid-1",
        ez_wallet_id="wallet-1",
        workspace_account_status="active",
    )
    db_session.add(user)
    await db_session.flush()

    raw_token = secrets.token_urlsafe(32)
    db_session.add(
        Session(
            user_id=user.id,
            token_hash=hashlib.sha256(raw_token.encode()).hexdigest(),
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
    )
    # commit, not flush: job_service._run's background task opens its own
    # session from app.state.sessionmaker on a separate connection, which
    # can't see this row until it's durably committed.
    await db_session.commit()

    app = create_app()
    app.dependency_overrides[get_db] = override_get_db(db_session)
    app.state.sessionmaker = async_sessionmaker(db_engine, expire_on_commit=False)
    return app, raw_token, user


@pytest.mark.asyncio
@respx.mock
async def test_submit_job_then_poll_until_done(app_and_token):
    app, token, _user = app_and_token
    respx.get("https://workspace.invalid/wallet/wallet-1/balance").mock(
        return_value=Response(200, json={"credit_balance": 100})
    )
    respx.post("https://flip.invalid/flip").mock(
        return_value=Response(200, json={"file_base64": "flipped", "status": "done"})
    )
    respx.post("https://workspace.invalid/wallet/wallet-1/debit").mock(
        return_value=Response(200, json={"credit_balance": 88, "status": "ok"})
    )

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        submit = await client.post(
            "/tools/jobs",
            json={
                "operation": "flip",
                "file_base64": "orig",
                "file_name": "a.pptx",
                "direction": "rtl",
                "expert_review": False,
                "idempotency_key": str(uuid.uuid4()),
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        assert submit.status_code == 200
        job_id = submit.json()["job_id"]

        poll = await client.get(
            f"/tools/jobs/{job_id}", headers={"Authorization": f"Bearer {token}"}
        )

    assert poll.status_code == 200
    body = poll.json()
    assert body["status"] == "done"
    assert body["result_file_base64"] == "flipped"
    assert body["credits_charged"] == 12


@pytest.mark.asyncio
@respx.mock
async def test_submit_job_repeat_idempotency_key_returns_same_job(app_and_token):
    app, token, _user = app_and_token
    respx.get("https://workspace.invalid/wallet/wallet-1/balance").mock(
        return_value=Response(200, json={"credit_balance": 100})
    )
    flip_route = respx.post("https://flip.invalid/flip").mock(
        return_value=Response(200, json={"file_base64": "x", "status": "done"})
    )
    respx.post("https://workspace.invalid/wallet/wallet-1/debit").mock(
        return_value=Response(200, json={"status": "ok"})
    )
    key = str(uuid.uuid4())
    body = {
        "operation": "flip",
        "file_base64": "orig",
        "file_name": "a.pptx",
        "direction": "rtl",
        "expert_review": False,
        "idempotency_key": key,
    }

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        first = await client.post(
            "/tools/jobs", json=body, headers={"Authorization": f"Bearer {token}"}
        )
        second = await client.post(
            "/tools/jobs", json=body, headers={"Authorization": f"Bearer {token}"}
        )

    assert first.json()["job_id"] == second.json()["job_id"]
    # only the first submission's background task should ever call flip
    assert flip_route.call_count <= 1


@pytest.mark.asyncio
@respx.mock
async def test_submit_job_insufficient_balance_returns_402_no_job_row(
    app_and_token, db_session
):
    app, token, _user = app_and_token
    respx.get("https://workspace.invalid/wallet/wallet-1/balance").mock(
        return_value=Response(200, json={"credit_balance": 1})
    )

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/tools/jobs",
            json={
                "operation": "flip",
                "file_base64": "orig",
                "file_name": "a.pptx",
                "direction": "rtl",
                "expert_review": False,
                "idempotency_key": str(uuid.uuid4()),
            },
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 402
    assert response.json()["error_code"] == "insufficient_balance"
    from sqlalchemy import select

    result = await db_session.execute(select(ToolJob))
    assert result.scalars().all() == []


@pytest.mark.asyncio
async def test_submit_job_unprovisioned_returns_503_before_balance_check(db_session):
    user = User(work_email="b@example.com", ms_oid="oid-2")  # default unprovisioned
    db_session.add(user)
    await db_session.flush()
    raw_token = secrets.token_urlsafe(32)
    db_session.add(
        Session(
            user_id=user.id,
            token_hash=hashlib.sha256(raw_token.encode()).hexdigest(),
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
    )
    await db_session.flush()

    app = create_app()
    app.dependency_overrides[get_db] = override_get_db(db_session)

    with respx.mock:  # no routes registered -- any Workspace call would raise
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post(
                "/tools/jobs",
                json={
                    "operation": "flip",
                    "file_base64": "orig",
                    "file_name": "a.pptx",
                    "direction": "rtl",
                    "expert_review": False,
                    "idempotency_key": str(uuid.uuid4()),
                },
                headers={"Authorization": f"Bearer {raw_token}"},
            )

    assert response.status_code == 503
    assert response.json()["error_code"] == "workspace_account_unprovisioned"


@pytest.mark.asyncio
async def test_poll_job_returns_real_persisted_step_not_hardcoded(db_session):
    """Code-review finding #9: the route must surface job_service._run's
    real, persisted step value while non-terminal, not a hardcoded
    "processing" placeholder for the whole window."""
    user = User(
        work_email="d@example.com",
        ms_oid="oid-4",
        ez_wallet_id="wallet-1",
        workspace_account_status="active",
    )
    db_session.add(user)
    await db_session.flush()

    raw_token = secrets.token_urlsafe(32)
    db_session.add(
        Session(
            user_id=user.id,
            token_hash=hashlib.sha256(raw_token.encode()).hexdigest(),
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
    )
    job = ToolJob(
        user_id=user.id,
        operation="translate",
        idempotency_key=str(uuid.uuid4()),
        input_file_meta={},
        status="processing",
        step="translating",
    )
    db_session.add(job)
    await db_session.flush()

    app = create_app()
    app.dependency_overrides[get_db] = override_get_db(db_session)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get(
            f"/tools/jobs/{job.id}", headers={"Authorization": f"Bearer {raw_token}"}
        )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "processing"
    assert body["step"] == "translating"


@pytest.mark.asyncio
async def test_poll_job_expired_but_within_grace_window_still_returns_job(db_session):
    user = User(
        work_email="c@example.com",
        ms_oid="oid-3",
        ez_wallet_id="wallet-1",
        workspace_account_status="active",
    )
    db_session.add(user)
    await db_session.flush()

    raw_token = secrets.token_urlsafe(32)
    db_session.add(
        Session(
            user_id=user.id,
            token_hash=hashlib.sha256(raw_token.encode()).hexdigest(),
            # expired 5 minutes ago -- well within the 30-minute grace window
            expires_at=datetime.now(UTC) - timedelta(minutes=5),
        )
    )
    job = ToolJob(
        user_id=user.id,
        operation="flip",
        idempotency_key=str(uuid.uuid4()),
        input_file_meta={},
        status="done",
        credits_cost=12,
    )
    db_session.add(job)
    await db_session.flush()

    app = create_app()
    app.dependency_overrides[get_db] = override_get_db(db_session)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get(
            f"/tools/jobs/{job.id}", headers={"Authorization": f"Bearer {raw_token}"}
        )

    assert response.status_code == 200
    assert response.json()["status"] == "done"
