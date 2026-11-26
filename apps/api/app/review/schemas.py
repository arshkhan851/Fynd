from datetime import datetime
from decimal import Decimal
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel


class PendingEvidenceItem(BaseModel):
    evidence_id: int
    competition_id: int
    canonical_title: str | None
    field_name: str
    field_key: str
    category_name: str | None
    category_slug: str | None
    observed_value: dict[str, Any]
    evidence_excerpt: str | None
    observed_at: datetime
    source_url: str | None
    publisher: str | None


class PendingEvidenceResponse(BaseModel):
    items: list[PendingEvidenceItem]
    limit: int
    field: str | None


class EvidenceReviewEventResponse(BaseModel):
    id: UUID
    reviewer_user_id: UUID
    action: Literal["accepted", "rejected", "superseded"]
    previous_state: Literal["candidate", "selected"]
    new_state: Literal["selected", "rejected", "superseded"]
    created_at: datetime


class EvidenceDetailItem(BaseModel):
    id: int
    field_name: str
    field_key: str
    category_name: str | None
    category_slug: str | None
    observed_value: dict[str, Any]
    evidence_excerpt: str | None
    confidence: Decimal | None
    observed_at: datetime
    review_state: Literal["candidate", "selected", "rejected", "superseded"]
    reviewed_at: datetime | None


class EvidenceDetailResponse(BaseModel):
    evidence: EvidenceDetailItem
    competition: dict[str, Any]
    source: dict[str, Any]
    review_events: list[EvidenceReviewEventResponse]


class EvidenceMutationResponse(BaseModel):
    evidence_id: int
    competition_id: int
    field_name: str
    field_key: str
    review_state: Literal["selected", "rejected"]
    reviewed_at: datetime | None
