"""API-01 response DTO. Mail bodies and provider credentials are never included."""
from typing import Literal
from uuid import UUID

from pydantic import Field, model_validator
from decimal import Decimal, InvalidOperation

from app.shared.domain.schemas import StrictModel

from app.features.work_cases.domain.hold import HoldInfo

QueueFilter = Literal['Waiting', 'Running', 'Complete', 'Pending', 'Confirm']
# 업무 상태 → 큐 탭. 화면 탭과 같다: 메일만 확인한 업무(Confirm)는 대기, 보류·승인 대기(Pending)는 진행 중
QUEUE_TABS = {'Waiting': 'Waiting', 'Confirm': 'Waiting', 'Running': 'Running', 'Pending': 'Running',
              'Complete': 'Complete'}


class StepDetail(StrictModel):
    code: str
    message: str


class ReplySummary(StrictModel):
    requested_supplier_count: int = Field(default=0, strict=True, ge=0)
    responded_supplier_count: int = Field(default=0, strict=True, ge=0)
    reply_rate: str | None = None

    @model_validator(mode='after')
    def validate_counts(self):
        if self.responded_supplier_count > self.requested_supplier_count:
            raise ValueError('Reply count exceeds successful requests')
        if self.requested_supplier_count == 0:
            if self.reply_rate is not None:
                raise ValueError('No requests requires a null reply rate')
        else:
            try:
                rate = Decimal(self.reply_rate) if self.reply_rate is not None else None
                if rate is None or not rate.is_finite() or not 0 <= rate <= 1:
                    raise ValueError('Invalid reply rate')
            except InvalidOperation:
                raise ValueError('Invalid reply rate') from None
        return self


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
    hold: HoldInfo | None = None
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
