from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.brief import Brief
from app.services.ref_number import generate_ref

# KD10: /brief is persistence + ref-number issuance only, no orchestration
# or upsell business logic -- the UI for this is post-MVP.
QUOTE_ETA_MINUTES = 10


async def create_brief(
    db: AsyncSession,
    *,
    user_id: UUID,
    division: str,
    capability: str | None,
    output_format: str | None,
    deadline: str | None,
    notes: str | None,
    upsells: list[str],
) -> Brief:
    brief = Brief(
        user_id=user_id,
        division=division,
        capability=capability,
        output_format=output_format,
        deadline=deadline,
        notes=notes,
        upsells=upsells,
        ref_number=generate_ref(),
    )
    db.add(brief)
    await db.flush()
    return brief
