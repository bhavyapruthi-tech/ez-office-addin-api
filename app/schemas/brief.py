from pydantic import BaseModel


class BriefRequest(BaseModel):
    division: str
    capability: str | None = None
    output_format: str | None = None
    deadline: str | None = None
    files: list[str] = []
    notes: str | None = None
    upsells: list[str] = []


class BriefResponse(BaseModel):
    ref_number: str
    quote_eta_minutes: int
