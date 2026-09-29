import secrets
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
import msal
from jwt import PyJWKClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.errors import UnauthorizedError
from app.core.security import hash_token
from app.models.session import Session
from app.models.user import User
from app.services.workspace_client import WorkspaceClient

_JWKS_URL = (
    f"https://login.microsoftonline.com/{settings.entra_tenant_id}/discovery/v2.0/keys"
)
# KTD2: PyJWKClient instantiated once at module scope, cached -- never
# re-fetch the JWKS per request.
_jwks_client = PyJWKClient(_JWKS_URL, cache_keys=True)

SESSION_TTL = timedelta(hours=12)


def validate_entra_token(
    token: str, *, expected_audience: str | None = None
) -> dict[str, Any]:
    """KTD2/KTD3: one generic validator for both the NAA and legacy-SSO
    flows. Hardcodes RS256 rather than trusting the token's own `alg`
    header (algorithm-confusion defense). Rejects a token from an
    unexpected tenant before any OBO exchange is attempted -- this
    backend is single-tenant by design.

    `expected_audience` defaults to the bare client ID (the incoming
    NAA/legacy-SSO token's own audience). The legacy path's second call,
    re-validating the freshly OBO-exchanged token, must pass the
    `api://{client_id}` resource URI instead -- that token's `aud` is the
    exposed API's resource identifier, not the bare client ID, since
    exchange_obo requests it against that scope.
    """
    try:
        signing_key = _jwks_client.get_signing_key_from_jwt(token)
        claims: dict[str, Any] = jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            audience=expected_audience or settings.entra_client_id,
            issuer=f"https://login.microsoftonline.com/{settings.entra_tenant_id}/v2.0",
        )
    except jwt.PyJWTError as exc:
        raise UnauthorizedError(f"Token validation failed: {exc}") from None

    if claims.get("tid") != settings.entra_tenant_id:
        raise UnauthorizedError("Token issued for an unexpected tenant.")

    return claims


def exchange_obo(token: str, claims: dict[str, Any]) -> str:
    """KTD3: authority is built from the incoming token's own `tid` claim
    -- never `/common` or `/organizations`, a documented multi-tenant OBO
    pitfall.
    """
    tenant_id = claims["tid"]
    app = msal.ConfidentialClientApplication(
        client_id=settings.entra_client_id,
        client_credential=settings.entra_client_secret,
        authority=f"https://login.microsoftonline.com/{tenant_id}",
    )
    result = app.acquire_token_on_behalf_of(
        user_assertion=token,
        scopes=[f"api://{settings.entra_client_id}/access_as_user"],
    )
    if "access_token" not in result:
        error_description = result.get("error_description", "OBO exchange failed")
        raise UnauthorizedError(error_description)
    return str(result["access_token"])


async def handle_auth_session(
    token: str,
    auth_mode: str,
    db: AsyncSession,
    workspace_client: WorkspaceClient,
) -> tuple[User, str]:
    """R1/R2: both auth_mode paths converge on the same users row
    creation + session issuance + Workspace provisioning (retried on
    every call until the user's status is "active", per R4/KD4).
    Returns (user, raw_session_token).
    """
    claims = validate_entra_token(token)

    if auth_mode == "legacy":
        access_token = exchange_obo(token, claims)
        claims = validate_entra_token(
            access_token, expected_audience=f"api://{settings.entra_client_id}"
        )

    ms_oid = claims["oid"]
    work_email = claims.get("preferred_username") or claims.get("email") or ""

    result = await db.execute(select(User).where(User.ms_oid == ms_oid))
    user = result.scalar_one_or_none()

    first_use = user is None
    if user is None:
        user = User(work_email=work_email, ms_oid=ms_oid)
        db.add(user)
        await db.flush()

    # R4/KD4: retry provisioning on every call until it succeeds, not just
    # on the user's very first-ever call -- otherwise a user whose first
    # Workspace attempt fails is stuck "unprovisioned" forever, since
    # `first_use` alone is never True again for them.
    needs_provisioning = first_use or user.workspace_account_status != "active"
    if needs_provisioning:
        try:
            provisioned = await workspace_client.lookup_or_login_or_create(
                work_email, ms_oid
            )
            user.ez_workspace_account_id = provisioned["workspace_account_id"]
            user.ez_wallet_id = provisioned["wallet_id"]
            user.workspace_account_status = "active"
        except Exception:
            # R1: /auth/session still succeeds even if Workspace is
            # unreachable -- the session and users row are created
            # regardless; wallet routes short-circuit until a retry
            # succeeds on the next open (KD4).
            user.workspace_account_status = "unprovisioned"

    raw_token = secrets.token_urlsafe(32)
    session = Session(
        user_id=user.id,
        token_hash=hash_token(raw_token),
        expires_at=datetime.now(UTC) + SESSION_TTL,
    )
    db.add(session)
    await db.flush()

    return user, raw_token
