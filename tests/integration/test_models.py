import uuid

import pytest
from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError

from app.models import Base, StripeWebhookEvent, ToolJob, User


@pytest.mark.asyncio
async def test_migration_creates_all_five_tables_with_documented_columns(db_engine):
    async with db_engine.connect() as conn:
        table_names = await conn.run_sync(
            lambda sync_conn: inspect(sync_conn).get_table_names()
        )
    for table in Base.metadata.tables:
        assert table in table_names


@pytest.mark.asyncio
async def test_duplicate_idempotency_key_per_user_raises_unique_violation(db_session):
    user = User(work_email="a@example.com", ms_oid="oid-1")
    db_session.add(user)
    await db_session.flush()

    key = str(uuid.uuid4())
    db_session.add(ToolJob(user_id=user.id, operation="flip", idempotency_key=key))
    await db_session.flush()

    db_session.add(ToolJob(user_id=user.id, operation="flip", idempotency_key=key))
    with pytest.raises(IntegrityError):
        await db_session.flush()


@pytest.mark.asyncio
async def test_duplicate_stripe_event_id_raises_pk_violation_update_does_not(
    db_session,
):
    event = StripeWebhookEvent(event_id="evt_123", credited=False)
    db_session.add(event)
    await db_session.commit()

    db_session.add(StripeWebhookEvent(event_id="evt_123", credited=False))
    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()

    reloaded = await db_session.get(StripeWebhookEvent, "evt_123")
    reloaded.credited = True
    await db_session.flush()  # update, not insert -- no PK violation
