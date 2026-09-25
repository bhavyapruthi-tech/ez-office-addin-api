from typing import Literal

from pydantic import BaseModel


class AuthSessionRequest(BaseModel):
    token: str
    auth_mode: Literal["naa", "legacy"]


class AuthUser(BaseModel):
    email: str
    credit_balance: int | None


class AuthSessionResponse(BaseModel):
    session_token: str
    user: AuthUser
