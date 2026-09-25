from pydantic import BaseModel


class WalletBalanceResponse(BaseModel):
    credit_balance: int


class TopupRequest(BaseModel):
    amount_usd: int


class TopupResponse(BaseModel):
    client_secret: str
