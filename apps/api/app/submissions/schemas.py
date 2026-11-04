"""Public request and owner-scoped response schemas for submissions."""

from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ConsentAcceptance(BaseModel):
    model_config = ConfigDict(extra="forbid")

    declaration_version: str
    declaration_text: str


class SubmissionCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str
    description: str
    organizer_name: str
    organizer_email: str
    organizer_contact_name: str | None = None
    organizer_contact_email: str | None = None
    organizer_contact_phone: str | None = None
    official_url: str
    registration_url: str
    registration_deadline_at: datetime | None = None
    registration_deadline_on: date | None = None
    event_starts_at: datetime | None = None
    event_starts_on: date | None = None
    event_ends_at: datetime | None = None
    event_ends_on: date | None = None
    event_timezone: str | None = None
    format: Literal["online", "offline", "hybrid"]
    location_name: str | None = None
    location_city: str | None = None
    location_region: str | None = None
    location_country_code: str | None = None
    registration_price_status: Literal["free", "paid", "unknown"]
    registration_fee_amount: Decimal | None = None
    registration_fee_currency: str | None = None
    organizer_profile_id: UUID | None = None
    category_ids: list[int] = Field(default_factory=list)
    primary_category_id: int | None = None
    consent: ConsentAcceptance


class SubmissionCategoryResponse(BaseModel):
    id: int
    name: str
    slug: str
    is_primary: bool


class SubmissionResponse(BaseModel):
    id: int
    status: str
    title: str
    description: str
    organizer_name: str
    official_url: str
    registration_url: str
    registration_deadline_at: datetime | None
    registration_deadline_on: date | None
    event_starts_at: datetime | None
    event_starts_on: date | None
    event_ends_at: datetime | None
    event_ends_on: date | None
    event_timezone: str | None
    format: str
    location_name: str | None
    location_city: str | None
    location_region: str | None
    location_country_code: str | None
    registration_price_status: str
    registration_fee_amount: Decimal | None
    registration_fee_currency: str | None
    categories: list[SubmissionCategoryResponse]
    created_at: datetime


class SubmissionListResponse(BaseModel):
    items: list[SubmissionResponse]
    total: int
    limit: int
    offset: int


class SubmissionStatus(StrEnum):
    pending = "pending"
    under_review = "under_review"
    approved = "approved"
    rejected = "rejected"
    withdrawn = "withdrawn"


class RejectSubmissionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=10, max_length=2000)


class ApproveSubmissionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    existing_competition_id: int | None = Field(default=None, strict=True, gt=0)
    existing_organizer_id: int | None = Field(default=None, strict=True, gt=0)


class AdminReviewEventResponse(BaseModel):
    id: int
    reviewer_user_id: UUID
    reviewer_email: str
    previous_status: str
    new_status: str
    note: str | None
    created_at: datetime


class AdminConsentResponse(BaseModel):
    declaration_version: str
    accepted_at: datetime
    declaration_text: str


class AdminSubmissionQueueItem(BaseModel):
    id: int
    status: SubmissionStatus
    title: str
    organizer_name: str
    submitter_email: str
    organizer_profile_id: UUID | None
    created_at: datetime
    updated_at: datetime
    review_events: list[AdminReviewEventResponse]


class AdminSubmissionQueueResponse(BaseModel):
    items: list[AdminSubmissionQueueItem]
    total: int
    limit: int
    offset: int


class AdminSubmissionDetailResponse(SubmissionResponse):
    submitter_email: str
    organizer_email: str
    organizer_contact_name: str | None
    organizer_contact_email: str | None
    organizer_contact_phone: str | None
    organizer_profile_id: UUID | None
    approved_competition_id: int | None
    consent: AdminConsentResponse
    review_events: list[AdminReviewEventResponse]
