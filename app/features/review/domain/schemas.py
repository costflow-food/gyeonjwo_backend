from pydantic import Field, StrictBool, model_validator
from app.shared.domain.schemas import StrictModel


FIELD_NAMES = (
    'supplier_name', 'item_name', 'order_unit', 'net_kg_per_order_unit', 'origin', 'grade',
    'processing', 'old_unit_price', 'new_unit_price', 'unit', 'currency', 'effective_date',
    'reason', 'requested_quantity', 'requested_delivery_date', 'transport_amount',
    'other_logistics_amount', 'cost_basis', 'cost_payer', 'cost_included_in_price',
)


class NotesRequest(StrictModel):
    notes: str | None = Field(default=None, max_length=8192)


class ExtractedField(StrictModel):
    field_name: str
    raw_value: str | None
    source_location: str = Field(min_length=1)


class Extraction(StrictModel):
    item_count: int = Field(ge=0)
    fields: list[ExtractedField]

    @model_validator(mode='after')
    def all_twenty(self):
        if len(self.fields) != 20 or {f.field_name for f in self.fields} != set(FIELD_NAMES):
            raise ValueError('Exactly twenty distinct extraction fields are required')
        return self


class Verdict(StrictModel):
    passed: StrictBool
    reason: str = Field(min_length=1)
    sources: list[str]


class ReferenceDecision(StrictModel):
    reference_id: str
    comparable: StrictBool
    reason: str = Field(min_length=1)
    unconfirmed_conditions: list[str]
    sources: list[str]


class ComparisonVerdict(StrictModel):
    decisions: list[ReferenceDecision]


class FormulaTerm(StrictModel):
    value_key: str
    quantity_key: str | None


class FormulaOption(StrictModel):
    option_id: str
    goods: list[FormulaTerm]
    transport_keys: list[str]
    other_logistics_keys: list[str]


class CalculationProposal(StrictModel):
    options: list[FormulaOption]


class CardText(StrictModel):
    option_id: str
    title: str
    explanation: str


class CardTexts(StrictModel):
    cards: list[CardText]


class ReviewSummary(StrictModel):
    summary: str
    sources: list[str]
