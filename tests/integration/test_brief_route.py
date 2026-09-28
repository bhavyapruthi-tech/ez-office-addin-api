import hashlib
import secrets
from datetime import UTC, datetime, timedelta

import pytest
from conftest import override_get_db
from httpx import ASGITransport, AsyncClient

from app.api.deps import get_db
from app.main import create_app
from app.models.brief import Brief
from app.models.session import Session
from app.models.user import User
from app.services import brief_service


@pytest.fixture
async def app_and_token(db_session):
    user = User(work_email="a@example.com", ms_oid="oid-1")
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
    return app, raw_token


@pytest.mark.asyncio
async def test_submit_brief_persists_and_returns_ref_number(app_and_token):
    app, token = app_and_token
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/brief",
            json={
                "division": "intelligence",
                "capability": "research_lab",
                "output_format": "pptx",
                "deadline": "this_week",
                "notes": "Focus on MENA market",
                "upsells": ["localization_center"],
            },
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["ref_number"].startswith("EZ-")
    assert body["quote_eta_minutes"] == 10


@pytest.mark.asyncio
async def test_submit_brief_missing_division_returns_422(app_and_token):
    app, token = app_and_token
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/brief", json={}, headers={"Authorization": f"Bearer {token}"}
        )

    assert response.status_code == 422
    body = response.json()
    assert body["error_code"] == "validation_error"
    assert isinstance(body["message"], str) and body["message"]


@pytest.mark.asyncio
async def test_create_brief_retries_on_ref_number_collision(db_session, monkeypatch):
    user = User(work_email="collision@example.com", ms_oid="oid-collision")
    db_session.add(user)
    await db_session.flush()

    existing = Brief(
        user_id=user.id,
        division="intelligence",
        capability=None,
        output_format=None,
        deadline=None,
        notes=None,
        upsells=[],
        ref_number="EZ-1234",
    )
    db_session.add(existing)
    await db_session.commit()

    ref_values = iter(["EZ-1234", "EZ-5678"])  # first collides, second is free
    monkeypatch.setattr(brief_service, "generate_ref", lambda: next(ref_values))

    brief = await brief_service.create_brief(
        db_session,
        user_id=user.id,
        division="intelligence",
        capability=None,
        output_format=None,
        deadline=None,
        notes=None,
        upsells=[],
    )

    assert brief.ref_number == "EZ-5678"
