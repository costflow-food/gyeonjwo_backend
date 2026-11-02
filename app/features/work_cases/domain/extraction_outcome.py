"""Backend persistence DTO, independent of agent-owned graph/schema classes."""
from typing import Literal
from uuid import UUID
from pydantic import Field
from app.shared.domain.schemas import StrictModel


class ExtractionStep(StrictModel):
    role: Literal['INFO_EXTRACTOR']
    next_action: Literal['STORED', 'HOLD']
    reason: str = Field(min_length=1, max_length=500)
    unknown_fields: list[str] = Field(default_factory=list)


class ExtractionOutcome(StrictModel):
    case_id: UUID
    input_revision: int = Field(strict=True, ge=1)
    status: Literal['EXTRACTION_READY', 'NEEDS_INFO', 'ON_HOLD', 'FAILED', 'STALE']
    processing_step: Literal['SELECTING_CANDIDATES', 'CANDIDATES', 'NEEDS_INFO', 'ON_HOLD', 'FAILED', 'EXTRACTING']
    draft_ids: list[str] = Field(default_factory=list, max_length=0)
    card_id: None = None
    unresolved: list[str] = Field(default_factory=list)
    steps: list[ExtractionStep] = Field(default_factory=list)
