"""API-13 견적 조회 응답. 금액·수량은 뒤 0과 지수 표기 없는 Decimal 문자열이다."""
from datetime import date
from decimal import Decimal, InvalidOperation
from uuid import UUID
from pydantic import Field
from app.shared.domain.schemas import StrictModel

MAX_QUOTE_ITEMS = 100


def decimal_text(value) -> str | None:
    """DB numeric·숫자 문자열을 '5500', '1000.5'처럼 바꾼다. 비었거나 숫자가 아니면 None."""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value).strip())
    except InvalidOperation:
        return None
    if not number.is_finite():
        return None
    text = format(number.normalize(), 'f')
    return '0' if text == '-0' else text


class QuoteCost(StrictModel):
    cost_type: str
    amount: str | None = None
    included_in_price: bool | None = None
    source_text: str | None = None


class QuoteCheck(StrictModel):
    field: str
    state: str
    value: str | None = None


class QuoteEvidence(StrictModel):
    field: str
    raw_value: str | None = None
    mail_id: UUID
    location: str | None = None


class QuoteMoq(StrictModel):
    quantity: str
    unit: str | None = None


class QuoteItem(StrictModel):
    quote_id: UUID
    version_no: int = Field(strict=True, ge=1)
    supplier_id: UUID
    supplier_name: str
    is_incumbent: bool
    source_mail_id: UUID
    extraction_status: str
    unit_price: str | None = None
    unit: str | None = None
    currency: str
    tax_basis: str | None = None
    available_quantity: str | None = None
    moq: QuoteMoq | None = None
    effective_from: date | None = None
    valid_until: date | None = None
    costs: list[QuoteCost] = Field(default_factory=list)
    checks: list[QuoteCheck] = Field(default_factory=list)
    item_notes: list[str] = Field(default_factory=list)
    evidence: list[QuoteEvidence] = Field(default_factory=list)


class QuotesResponse(StrictModel):
    case_id: UUID
    items: list[QuoteItem] = Field(default_factory=list, max_length=MAX_QUOTE_ITEMS)
