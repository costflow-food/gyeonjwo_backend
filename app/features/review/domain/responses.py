"""Public response models. Decimal fields serialize to JSON strings."""
from datetime import date, datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field, model_validator
from app.features.review.domain.schemas import FIELD_NAMES


class WorkResponse(BaseModel):
    work_id: str
    received_at: datetime
    sender: str
    subject: str
    classification_reason: str | None
    notes: str | None
    queue_status: Literal['PENDING', 'TRUE', 'FALSE', 'RUNNING', 'COMPLETE']
    item_id: str | None
    created_at: datetime
    updated_at: datetime


class WorkDetailResponse(BaseModel):
    work: WorkResponse
    mail_header: dict[str, str]


class QueueChangeResponse(BaseModel):
    work_id: str
    queue_status: Literal['PENDING', 'TRUE', 'FALSE', 'RUNNING', 'COMPLETE']
    classification_reason: str | None = None


class NotesResponse(BaseModel):
    work_id: str
    notes: str | None


class FileEntry(BaseModel):
    filename: str
    original_name: str
    content_type: str
    size_bytes: int


class FileListResponse(BaseModel):
    work_id: str
    files: list[FileEntry]


class Amounts(BaseModel):
    goods_amount: Decimal
    transport_amount: Decimal | None
    other_logistics_amount: Decimal | None
    total_cost: Decimal | None
    difference_from_before: Decimal | None

    @model_validator(mode='after')
    def consistent_total(self):
        parts = (self.goods_amount, self.transport_amount, self.other_logistics_amount)
        if any(value is not None and (not value.is_finite() or value < 0) for value in parts):
            raise ValueError('Amounts must be finite and nonnegative')
        if any(value is None for value in parts):
            if self.total_cost is not None or self.difference_from_before is not None:
                raise ValueError('Unconfirmed costs cannot have a total or difference')
        elif self.total_cost != sum(parts):
            raise ValueError('Total must equal all confirmed cost components')
        return self


class Baseline(Amounts):
    type: Literal['BEFORE']


class Card(Amounts):
    option_id: str
    type: Literal['AFTER', 'CANDIDATE']
    supplier_id: str
    supplier_name: str
    reference_id: str | None
    fixed_unit_price: Decimal = Field(ge=0)
    changeable_unit_price: Decimal = Field(ge=0)
    title: str
    explanation: str
    formula: str
    calculation_status: Literal['VERIFIED', 'COST_UNCONFIRMED']
    unconfirmed_conditions: list[str]
    sources: list[str]

    @model_validator(mode='after')
    def confirmed_status(self):
        if (self.calculation_status == 'VERIFIED') != (self.total_cost is not None):
            raise ValueError('Calculation status must match confirmed costs')
        return self


class AnalysisResponse(BaseModel):
    work_id: str
    queue_status: Literal['COMPLETE']
    outcome: Literal['SUCCESS', 'REVIEW_REQUIRED', 'FAILED']
    item_id: str | None = None
    cards: list[Card]
    issues: list[dict]
    result_persisted: Literal[False]
    unit: Literal['kg'] | None = None
    currency: Literal['KRW'] | None = None
    period: dict[str, str] | None = None
    quantities: dict[str, Decimal] | None = None
    baseline: Baseline | None = None
    extracted_fields: list[dict] | None = None
    candidate_search: dict | None = None
    calculation_verification: dict | None = None
    sources: list[str] | None = None
    error: dict | None = None

    @model_validator(mode='after')
    def outcome_contract(self):
        if self.outcome == 'SUCCESS':
            if not self.item_id or self.unit != 'kg' or self.currency != 'KRW' or self.baseline is None or self.quantities is None or self.period is None:
                raise ValueError('Successful analysis needs verified context and baseline')
            after = [card for card in self.cards if card.type == 'AFTER']
            candidates = [card for card in self.cards if card.type == 'CANDIDATE']
            if len(after) != 1 or len(candidates) > 2 or len({c.option_id for c in self.cards}) != len(self.cards):
                raise ValueError('Expected one AFTER and at most two distinct candidates')
            if set(self.quantities) != {'fixed', 'changeable', 'total'} or any(not value.is_finite() or value < 0 for value in self.quantities.values()) or self.quantities['total'] <= 0:
                raise ValueError('Quantities must be explicit, finite and nonnegative with a positive total')
            if self.quantities['fixed'] + self.quantities['changeable'] != self.quantities['total']:
                raise ValueError('Quantities must be conserved')
            if self.baseline.total_cost is None or self.baseline.difference_from_before != 0 or after[0].calculation_status != 'VERIFIED':
                raise ValueError('Baseline and AFTER must have confirmed costs')
            if len({c.supplier_id for c in candidates}) != len(candidates) or any(c.supplier_id == after[0].supplier_id for c in candidates):
                raise ValueError('Candidate suppliers must be distinct alternatives')
            if set(self.period) != {'from', 'to'} or date.fromisoformat(self.period['from']) > date.fromisoformat(self.period['to']):
                raise ValueError('Analysis period must be ordered ISO dates')
            fields = self.extracted_fields or []
            if len(fields) != 20 or {field.get('field_name') for field in fields} != set(FIELD_NAMES):
                raise ValueError('Successful analysis needs all twenty extracted fields')
            verification = self.calculation_verification or {}
            if type(verification.get('rounds')) is not int or verification['rounds'] < 2 or verification.get('all_rounds_match') is not True or verification.get('independent_check_passed') is not True:
                raise ValueError('Successful analysis needs independent verified calculations')
            for card in self.cards:
                if card.total_cost is not None and card.difference_from_before != card.total_cost - self.baseline.total_cost:
                    raise ValueError('Card difference must match the baseline')
        else:
            if self.cards:
                raise ValueError('Failed or review-required analysis cannot publish normal cards')
            if self.outcome == 'REVIEW_REQUIRED' and not self.issues:
                raise ValueError('Review-required analysis needs issues')
            if self.outcome == 'FAILED' and not self.error:
                raise ValueError('Failed analysis needs an error')
        return self


class ErrorResponse(BaseModel):
    error: dict
