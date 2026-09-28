from uuid import UUID

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.brief import Brief
from app.services.ref_number import generate_ref

# KD10: /brief is persistence + ref-number issuance only, no orchestration
# or upsell business logic -- the UI for this is post-MVP.
QUOTE_ETA_MINUTES = 10

# ref_number draws from only 9000 values (EZ-1000..EZ-9999) -- a collision
# is a normal event at this volume (~50% odds by the ~112th brief ever
# submitted), not a rare edge case, so retrying is load-bearing, not
# defensive. A collision rolls back the whole session, not just this
# attempt -- safe because create_brief is always the first write on any
# session it's given (its only real caller, POST /brief, commits nothing
# beforehand).
MAX_REF_NUMBER_ATTEMPTS = 5


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
    for attempt in range(MAX_REF_NUMBER_ATTEMPTS):
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
        try:
            await db.flush()
        except IntegrityError:
            await db.rollback()
            if attempt == MAX_REF_NUMBER_ATTEMPTS - 1:
                raise
            continue
        return brief
    raise AssertionError("unreachable")  # loop always returns or raises
