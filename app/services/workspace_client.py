from typing import Any, Literal

import httpx

from app.core.config import settings


class WorkspaceClient:
    """Client for the EZ Workspace Identity+Wallet API.

    KD1: this backend specifies the contract; Workspace builds to it, not
    the other way around. See ARCHITECTURE.md sec6.
    """

    def __init__(self, base_url: str | None = None, api_key: str | None = None):
        self._base_url = base_url or settings.workspace_api_base
        self._api_key = api_key or settings.workspace_api_key

    async def _request(
        self, method: str, path: str, *, json: dict[str, Any], timeout: float
    ) -> dict[str, Any]:
        async with httpx.AsyncClient(
            base_url=self._base_url, timeout=timeout
        ) as client:
            response = await client.request(
                method,
                path,
                json=json,
                headers={"Authorization": f"Bearer {self._api_key}"},
            )
            response.raise_for_status()
            return response.json()

    async def lookup_or_login_or_create(
        self, work_email: str, ms_oid: str
    ) -> dict[str, Any]:
        # KD1: 15-second timeout -- long enough for a real provisioning
        # call, short enough that a degraded Workspace doesn't hold
        # request-handling capacity open indefinitely.
        return await self._request(
            "POST",
            "/accounts",
            json={"work_email": work_email, "ms_oid": ms_oid},
            timeout=settings.workspace_provisioning_timeout_seconds,
        )

    async def get_balance(self, wallet_id: str) -> int:
        async with httpx.AsyncClient(base_url=self._base_url, timeout=10.0) as client:
            response = await client.get(
                f"/wallet/{wallet_id}/balance",
                headers={"Authorization": f"Bearer {self._api_key}"},
            )
            response.raise_for_status()
            return int(response.json()["credit_balance"])

    async def debit(
        self, wallet_id: str, amount: int, idempotency_key: str, reason: str = "job"
    ) -> dict[str, Any]:
        return await self._request(
            "POST",
            f"/wallet/{wallet_id}/debit",
            json={
                "amount": amount,
                "idempotency_key": idempotency_key,
                "reason": reason,
            },
            timeout=10.0,
        )

    async def credit(
        self,
        wallet_id: str,
        amount: int,
        idempotency_key: str,
        reason: Literal["topup", "refund"] = "topup",
    ) -> dict[str, Any]:
        return await self._request(
            "POST",
            f"/wallet/{wallet_id}/credit",
            json={
                "amount": amount,
                "idempotency_key": idempotency_key,
                "reason": reason,
            },
            timeout=10.0,
        )

    async def find_debit_by_idempotency_key(
        self, wallet_id: str, idempotency_key: str
    ) -> dict[str, Any] | None:
        """KD6: used by the recurring sweep to distinguish "never debited"
        (stale_timeout) from "debited, only our status write failed"
        (debit_succeeded_status_write_failed) before reconciling a
        stuck job. Re-calling `debit` with the same idempotency_key is
        itself safe (Workspace's own idempotency guarantee, sec6), so this
        is implemented as a defensive re-call rather than a separate
        lookup endpoint the contract doesn't otherwise need.
        """
        body = await self._request(
            "POST",
            f"/wallet/{wallet_id}/debit",
            json={"amount": 0, "idempotency_key": idempotency_key, "reason": "job"},
            timeout=10.0,
        )
        return body if body.get("status") == "ok" else None


workspace_client = WorkspaceClient()
