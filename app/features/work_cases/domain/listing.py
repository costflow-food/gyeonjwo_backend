"""API-01 response DTO. Mail bodies and provider credentials are never included."""
from typing import Literal
from uuid import UUID

from pydantic import Field

from app.shared.domain.schemas import StrictModel

QueueFilter = Literal['Waiting', 'Running', 'Complete']


class StepDetail(StrictModel):
    code: str
    message: str


class ReplySummary(StrictModel):
    requested_supplier_count: int = 0
    responded_supplier_count: int = 0
    reply_rate: str | None = None


class WorkCaseRow(StrictModel):
    case_id: UUID
    initial_mail_id: UUID
    title: str
    sender: str
    sender_name: str | None = None
    requester_name: str | None = None
    received_at: str
    status: Literal['Waiting', 'Running', 'Complete', 'Pending', 'Confirm']
    processing_step: str
    input_revision: int = Field(ge=1)
    archived_at: str | None
    item_name: str | None
    attachment_count: int = Field(ge=0)
    step_detail: StepDetail
    reply_summary: ReplySummary = Field(default_factory=ReplySummary)
    allowed_actions: list[str]
    disabled_actions: list[dict] = Field(default_factory=list)


class QueueCounts(StrictModel):
    Waiting: int = 0
    Running: int = 0
    Complete: int = 0
    total: int = 0


class WorkCaseList(StrictModel):
    items: list[WorkCaseRow]
    next_cursor: str | None
    counts: QueueCounts
    intake_pending: int = Field(ge=0)
