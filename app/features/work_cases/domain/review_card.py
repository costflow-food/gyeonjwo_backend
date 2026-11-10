"""API-14 검토 카드 응답. 금액·수량·단가는 Decimal 문자열이다."""
from typing import Literal
from uuid import UUID
from pydantic import Field
from app.shared.domain.schemas import StrictModel

# DB(review_card_check)가 카드 저장 때 정해 강제하는 종료 코드를 그대로 보낸다 (2026-11-06 결정)
ReviewOutcome = Literal['COMPLETED', 'REVIEW_REQUIRED_CLOSED', 'NO_VALID_PLAN']
REVIEW_ACTIONS = ('SELECT_PLAN', 'HOLD', 'CONFIRM_REVIEW')


class PlanAllocation(StrictModel):
    supplier_id: UUID
    supplier_name: str
    quote_id: UUID
    quantity: str
    unit: str | None = None
    unit_price: str | None = None


class ReviewPlan(StrictModel):
    plan_id: str | None = None   # 없으면 선택할 수 없는 안 (예전 형식 카드)
    plan_type: Literal['KEEP', 'SWITCH', 'SPLIT']
    validation_status: Literal['VALID', 'CONDITIONAL', 'INVALID', 'NEEDS_INFO']
    total_amount: str | None = None
    currency: str = 'KRW'
    allocations: list[PlanAllocation] = Field(min_length=1, max_length=2)
    conditions: list[str] = Field(default_factory=list)
    adjustments: list[str] = Field(default_factory=list)
    reason: str | None = None


class CounterTarget(StrictModel):
    supplier_id: UUID
    supplier_name: str
    target_unit_price: str
    conditions: list[str] = Field(default_factory=list)


class CounterReference(StrictModel):
    status: str
    unit: str | None = None
    requested_unit_price: str | None = None
    previous_unit_price: str | None = None
    ceiling_unit_price: str | None = None
    alternative_total: str | None = None
    allowed_range: list[str] = Field(default_factory=list)


class CounterOffer(StrictModel):
    status: Literal['PROPOSED', 'SKIPPED', 'HOLD']
    reason: str | None = None
    target: CounterTarget | None = None
    reference: CounterReference | None = None
    draft_ids: list[str] = Field(default_factory=list)


class ReviewQuestion(StrictModel):
    supplier_id: UUID
    supplier_name: str
    field: str
    priority: str
    reason: str
    break_even_amount: str | None = None
    formula: str | None = None


class ReviewDecision(StrictModel):
    decision: str
    plan_id: str | None = None
    note: str | None = None
    decided_at: str | None = None


class DisabledAction(StrictModel):
    action: str
    reason_code: str
    message: str


class ReviewCardResponse(StrictModel):
    case_id: UUID
    review_revision: int = Field(strict=True, ge=1)
    input_revision: int = Field(strict=True, ge=0)
    is_stale: bool
    created_at: str | None = None
    source_quote_ids: list[UUID] = Field(default_factory=list)
    review_outcome: ReviewOutcome
    closure_reason: str | None = None   # REVIEW_REQUIRED_CLOSED의 종료 사유
    plans: list[ReviewPlan] = Field(default_factory=list)
    counter_offer: CounterOffer | None = None
    questions: list[ReviewQuestion] = Field(default_factory=list)
    unresolved: list[str] = Field(default_factory=list)
    decision: ReviewDecision | None = None
    allowed_actions: list[str] = Field(default_factory=list)
    disabled_actions: list[DisabledAction] = Field(default_factory=list)
