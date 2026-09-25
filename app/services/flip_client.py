from typing import Any

import httpx

from app.core.config import settings


class FlipClient:
    """Placeholder EZ Flip API client -- pending real docs from the EZ team
    (ARCHITECTURE.md sec6). Assumed synchronous, base64 body, static API key.
    """

    async def flip(self, file_base64: str, direction: str) -> dict[str, Any]:
        async with httpx.AsyncClient(
            base_url=settings.flip_api_base, timeout=60.0
        ) as client:
            response = await client.post(
                "/flip",
                json={"file_base64": file_base64, "direction": direction},
                headers={"Authorization": f"Bearer {settings.flip_api_key}"},
            )
            response.raise_for_status()
            return response.json()


flip_client = FlipClient()
