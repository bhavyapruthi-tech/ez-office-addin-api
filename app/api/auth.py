from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db
from app.schemas.auth import AuthSessionRequest, AuthSessionResponse, AuthUser
from app.services import auth_service
from app.services.workspace_client import workspace_client

router = APIRouter()


@router.post("/auth/session", response_model=AuthSessionResponse)
async def create_session(
    body: AuthSessionRequest, db: AsyncSession = Depends(get_db)
) -> AuthSessionResponse:
    user, raw_token = await auth_service.handle_auth_session(
        body.token, body.auth_mode, db, workspace_client
    )
    await db.commit()

    credit_balance = None
    if user.workspace_account_status == "active" and user.ez_wallet_id:
        try:
            credit_balance = await workspace_client.get_balance(user.ez_wallet_id)
        except Exception:  # noqa: BLE001 -- balance display is best-effort here
            credit_balance = None

    return AuthSessionResponse(
        session_token=raw_token,
        user=AuthUser(email=user.work_email, credit_balance=credit_balance),
    )
