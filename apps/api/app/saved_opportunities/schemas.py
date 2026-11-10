from datetime import date, datetime

from pydantic import BaseModel, ConfigDict


class SavedOpportunityItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    competition_id: int
    title: str
    format: str | None
    event_starts_on: date | None
    event_starts_at: datetime | None
    registration_deadline_on: date | None
    registration_deadline_at: datetime | None
    saved_at: datetime


class SavedOpportunityListResponse(BaseModel):
    items: list[SavedOpportunityItem]
    total: int
    limit: int
    offset: int


class SavedOpportunityMutationResponse(BaseModel):
    competition_id: int
    saved: bool
