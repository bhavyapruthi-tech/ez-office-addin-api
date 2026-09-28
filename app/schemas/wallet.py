from pydantic import BaseModel, Field


class WalletBalanceResponse(BaseModel):
    credit_balance: int


class TopupRequest(BaseModel):
    amount_usd: int = Field(gt=0)


class TopupResponse(BaseModel):
    client_secret: str
