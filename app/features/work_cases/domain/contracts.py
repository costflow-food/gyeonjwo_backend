"""08 work-case domain contracts. Not wired to the legacy work_items API/DB.

Repository implementations must apply these guards inside the same transaction
as the write. A check followed by an independent UPDATE is not concurrency safe.
"""
from enum import StrEnum
from uuid import UUID

from pydantic import ConfigDict, Field, model_validator

from app.core.errors import ServiceError
from app.shared.domain.schemas import StrictModel


class CaseStatus(StrEnum):
    WAITING = 'Waiting'
    RUNNING = 'Running'
    COMPLETE = 'Complete'


class Decision(StrEnum):
    ACCEPT_INCREASE = 'ACCEPT_INCREASE'
    SELECT_PLAN = 'SELECT_PLAN'
    HOLD = 'HOLD'
    CONFIRM_REVIEW = 'CONFIRM_REVIEW'


class Action(StrEnum):
    VIEW_ORIGINAL = 'VIEW_ORIGINAL'
    START = 'START'
    ACCEPT_INCREASE = 'ACCEPT_INCREASE'
    EDIT_CONDITIONS = 'EDIT_CONDITIONS'
    SELECT_CANDIDATES = 'SELECT_CANDIDATES'
    APPROVE_SEND = 'APPROVE_SEND'
    EDIT_DRAFT = 'EDIT_DRAFT'
    EDIT_SEND_SETTINGS = 'EDIT_SEND_SETTINGS'
    RECONCILE_SEND = 'RECONCILE_SEND'
    RESUME = 'RESUME'
    SELECT_PLAN = 'SELECT_PLAN'
    HOLD = 'HOLD'
    CONFIRM_REVIEW = 'CONFIRM_REVIEW'
    ARCHIVE = 'ARCHIVE'
    RESTORE = 'RESTORE'
    RESOLVE_LINK = 'RESOLVE_LINK'
    SEARCH_SUPPLIERS = 'SEARCH_SUPPLIERS'


class RevisionRequest(StrictModel):
    expected_revision: int = Field(strict=True, ge=0)


class DisabledAction(StrictModel):
    action: Action
    reason_code: str = Field(min_length=1)
    message: str = Field(min_length=1)


class ActionAvailability(StrictModel):
    allowed_actions: list[Action]
    disabled_actions: list[DisabledAction]

    @model_validator(mode='after')
    def disjoint_actions(self):
        allowed = self.allowed_actions
        disabled = [item.action for item in self.disabled_actions]
        if len(set(allowed)) != len(allowed) or len(set(disabled)) != len(disabled):
            raise ValueError('Duplicate action')
        if set(allowed) & set(disabled):
            raise ValueError('An action cannot be both allowed and disabled')
        return self


class CaseSnapshot(StrictModel):
    """Minimal stored facts for guards; not the full API-02 response DTO."""
    model_config = ConfigDict(extra='forbid', frozen=True)
    case_id: UUID
    status: CaseStatus
    processing_step: str = Field(min_length=1)
    input_revision: int = Field(strict=True, ge=0)
    review_revision: int | None = Field(strict=True, ge=0)
    decision: Decision | None
    archived: bool = Field(strict=True)

    @model_validator(mode='after')
    def state_boundary(self):
        if self.processing_step in {'FAILED', 'WAITING_REPLY'} and self.status != CaseStatus.RUNNING:
            raise ValueError('Failure and reply wait must remain Running')
        if self.processing_step == 'REVIEW_READY' and self.status != CaseStatus.COMPLETE:
            raise ValueError('A stored review card is Complete')
        if self.processing_step == 'REVIEW_READY' and self.review_revision is None:
            raise ValueError('A stored review card must identify its revision')
        if self.status == CaseStatus.WAITING and self.processing_step != 'PENDING_START':
            raise ValueError('Waiting has not started processing')
        return self


def require_revision(expected: int, current: int) -> None:
    if type(expected) is not int or type(current) is not int or min(expected, current) < 0:
        raise ValueError('Revisions must be nonnegative integers')
    if expected != current:
        raise ServiceError('REVISION_CONFLICT', '최신 내용을 확인해 주세요.', 409,
                           current_revision=current, retryable=False)


def require_first_start(case: CaseSnapshot, expected_revision: int) -> None:
    require_revision(expected_revision, case.input_revision)
    if case.archived or case.decision is not None:
        raise ServiceError('ACTION_NOT_ALLOWED', '수긍·보관된 업무는 명시적으로 재개해 주세요.', 409)
    if case.status != CaseStatus.WAITING:
        raise ServiceError('WORK_ALREADY_STARTED', '이미 실행된 업무입니다.', 409)


class PlanSnapshot(StrictModel):
    model_config = ConfigDict(extra='forbid', frozen=True)
    case_id: UUID
    plan_id: UUID
    input_revision: int = Field(strict=True, ge=0)
    review_revision: int = Field(strict=True, ge=0)
    validation_status: str = Field(pattern=r'^(VALID|CONDITIONAL|INVALID|NEEDS_INFO)$')


def require_plan_selection(case: CaseSnapshot, plan: PlanSnapshot, expected_revision: int) -> None:
    require_revision(expected_revision, case.input_revision)
    if case.archived or case.status != CaseStatus.COMPLETE or case.decision == Decision.ACCEPT_INCREASE:
        raise ServiceError('ACTION_NOT_ALLOWED', '현재 상태에서 구매안을 선택할 수 없습니다.', 409)
    # review_revision is a card version, not an input revision. Compare each to its own source.
    if (plan.case_id != case.case_id or plan.input_revision != case.input_revision
            or plan.review_revision != case.review_revision or plan.validation_status != 'VALID'):
        raise ServiceError('INVALID_PLAN_SELECTION', '최신 검토의 유효한 구매안을 선택해 주세요.', 422,
                           retryable=False, next_action='REFRESH_CASE')
