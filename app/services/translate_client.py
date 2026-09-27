from typing import Any

import httpx

from app.core.config import settings


class TranslateClient:
    """Placeholder EZ Machine Translation API client -- pending real docs
    from the EZ team (ARCHITECTURE.md sec6). Assumed synchronous, base64
    body, static API key.
    """

    async def translate(
        self, file_base64: str, lang_from: str, lang_to: str
    ) -> dict[str, Any]:
        async with httpx.AsyncClient(
            base_url=settings.translate_api_base, timeout=60.0
        ) as client:
            response = await client.post(
                "/translate",
                json={
                    "file_base64": file_base64,
                    "lang_from": lang_from,
                    "lang_to": lang_to,
                },
                headers={"Authorization": f"Bearer {settings.translate_api_key}"},
            )
            response.raise_for_status()
            return response.json()


translate_client = TranslateClient()
