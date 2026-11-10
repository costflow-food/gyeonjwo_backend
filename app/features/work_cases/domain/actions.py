from typing import Literal
from uuid import UUID
from pydantic import Field
from app.shared.domain.schemas import StrictModel


class DeletedCase(StrictModel):
    case_id: UUID
    deleted: Literal[True]


class StartedCase(StrictModel):
    case_id: UUID
    input_revision: int = Field(strict=True, ge=1)
    batch_id: UUID
    progress_url: str
