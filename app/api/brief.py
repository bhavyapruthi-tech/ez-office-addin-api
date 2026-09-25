from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_session, get_db
from app.models.user import User
from app.schemas.brief import BriefRequest, BriefResponse
from app.services import brief_service

router = APIRouter()


@router.post("/brief", response_model=BriefResponse)
async def submit_brief(
    body: BriefRequest,
    user: User = Depends(get_current_session),
    db: AsyncSession = Depends(get_db),
) -> BriefResponse:
    brief = await brief_service.create_brief(
        db,
        user_id=user.id,
        division=body.division,
        capability=body.capability,
        output_format=body.output_format,
        deadline=body.deadline,
        notes=body.notes,
        upsells=body.upsells,
    )
    await db.commit()
    return BriefResponse(
        ref_number=brief.ref_number,
        quote_eta_minutes=brief_service.QUOTE_ETA_MINUTES,
    )
