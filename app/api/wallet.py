from fastapi import APIRouter, Depends

from app.api.deps import get_current_session
from app.core.errors import WorkspaceUnprovisionedError
from app.models.user import User
from app.schemas.wallet import TopupRequest, TopupResponse, WalletBalanceResponse
from app.services import stripe_service, wallet_service
from app.services.workspace_client import workspace_client

router = APIRouter()


@router.get("/wallet", response_model=WalletBalanceResponse)
async def get_wallet(
    user: User = Depends(get_current_session),
) -> WalletBalanceResponse:
    balance = await wallet_service.get_balance(user, workspace_client)
    return WalletBalanceResponse(credit_balance=balance)


@router.post("/wallet/topup", response_model=TopupResponse)
async def topup_wallet(
    body: TopupRequest, user: User = Depends(get_current_session)
) -> TopupResponse:
    if user.workspace_account_status != "active" or not user.ez_wallet_id:
        raise WorkspaceUnprovisionedError()
    client_secret = stripe_service.create_payment_intent(
        body.amount_usd, user.ez_wallet_id
    )
    return TopupResponse(client_secret=client_secret)
