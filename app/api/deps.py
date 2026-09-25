import hashlib
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

from fastapi import Depends, Header, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.errors import UnauthorizedError
from app.models.session import Session
from app.models.user import User


async def get_db(request: Request) -> AsyncIterator[AsyncSession]:
    async with request.app.state.sessionmaker() as session:
        yield session


def _hash_token(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode()).hexdigest()


async def _load_session_and_user(
    authorization: str | None, db: AsyncSession, *, allow_expired: bool
) -> tuple[Session, User]:
    if not authorization or not authorization.startswith("Bearer "):
        raise UnauthorizedError()
    raw_token = authorization.removeprefix("Bearer ")
    token_hash = _hash_token(raw_token)

    result = await db.execute(select(Session).where(Session.token_hash == token_hash))
    session = result.scalar_one_or_none()
    if session is None or session.revoked_at is not None:
        raise UnauthorizedError()

    now = datetime.now(UTC)
    if session.expires_at < now:
        # KD7: GET /tools/jobs/{id} tolerates a token up to 30 minutes past
        # `exp` so an owner can always retrieve their own job's terminal
        # state through a lapsed session -- every other route enforces
        # `exp` strictly.
        grace = timedelta(seconds=settings.session_expired_grace_seconds)
        if not allow_expired or session.expires_at + grace < now:
            raise UnauthorizedError()

    user_result = await db.execute(select(User).where(User.id == session.user_id))
    user = user_result.scalar_one_or_none()
    if user is None:
        raise UnauthorizedError()

    return session, user


async def get_current_session(
    authorization: str | None = Header(default=None),
    db: AsyncSession = Depends(get_db),
) -> User:
    _session, user = await _load_session_and_user(
        authorization, db, allow_expired=False
    )
    return user


async def get_current_session_allow_expired(
    authorization: str | None = Header(default=None),
    db: AsyncSession = Depends(get_db),
) -> User:
    """KD7: `GET /tools/jobs/{id}` only -- see `_load_session_and_user`."""
    _session, user = await _load_session_and_user(authorization, db, allow_expired=True)
    return user
