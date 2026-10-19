"""Pure business rules, independent of FastAPI and external services."""

from datetime import date
from decimal import Decimal, InvalidOperation
import re

from .errors import AppError

FIELD_NAMES = (
    "supplier_name",
    "item_name",
    "specification",
    "origin",
    "grade",
    "processing",
    "old_unit_price",
    "new_unit_price",
    "unit",
    "currency",
    "effective_date",
    "reason",
    "requested_quantity",
    "requested_delivery_date",
    "transport_amount",
    "other_logistics_amount",
    "cost_basis",
    "cost_payer",
    "cost_included_in_price",
)


def number(value) -> Decimal | None:
    if value is None:
        return None
    raw = str(value).strip().replace(",", "")
    if len(raw) > 40:
        return None
    if not re.fullmatch(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)", raw):
        return None
    try:
        result = Decimal(raw)
        return result if result.is_finite() else None
    except InvalidOperation:
        return None


def iso_date(value: str | None) -> date | None:
    if not value or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value.strip()):
        return None
    try:
        return date.fromisoformat(value.strip())
    except ValueError:
        return None


def line_values(fields: list[dict]) -> dict[int, dict]:
    lines: dict[int, dict] = {}
    for f in fields:
        lines.setdefault(f["line_no"], {})[f["field_name"]] = f.get("raw_value")
    return lines


def landed_price(unit_price, terms: list[dict], as_of: date | None = None) -> dict:
    price = number(unit_price)
    problems = []
    if price is None or price < 0:
        return {"landed_unit_price": None, "issues": ["단가가 없거나 유효하지 않습니다."]}
    extra = Decimal(0)
    for kind in ("TRANSPORT", "OTHER_LOGISTICS"):
        matches = [t for t in terms if t["cost_type"] == kind]
        if len(matches) != 1:
            problems.append(f"{kind}: 비용 조건이 확인되지 않았습니다.")
            continue
        t = matches[0]
        coverage_from = iso_date(t.get("coverage_from"))
        coverage_to = iso_date(t.get("coverage_to"))
        if (coverage_from or coverage_to) and (
            as_of is None
            or (coverage_from and as_of < coverage_from)
            or (coverage_to and as_of > coverage_to)
        ):
            problems.append(f"{kind}: 적용일에 유효한 비용 조건인지 확인할 수 없습니다.")
            continue
        if t["included_in_price"] == "TRUE" or t["payer"] == "SUPPLIER":
            continue
        if t["included_in_price"] != "FALSE" or t["payer"] != "BUYER":
            problems.append(f"{kind}: 비용 포함 여부 또는 부담자가 미확정입니다.")
            continue
        amount = number(t.get("amount"))
        if amount is None or amount < 0:
            problems.append(f"{kind}: 유효한 비용 금액이 필요합니다.")
        elif t["basis"] == "PER_UNIT":
            extra += amount
        elif t["basis"] == "TOTAL":
            coverage = number(t.get("coverage_quantity"))
            if coverage is None or coverage <= 0:
                problems.append(f"{kind}: 총액 배분에 적용할 수량이 없습니다.")
            else:
                extra += amount / coverage
        else:
            problems.append(f"{kind}: 비용 산정 기준이 미확정입니다.")
    return {
        "landed_unit_price": str(price + extra) if not problems else None,
        "issues": problems,
        "unit_currency_verified": False,
        "note": "기준자료에는 단위·통화 컬럼이 없으므로 검토자가 별도로 확인해야 합니다.",
    }


def validate_quote(requested: dict, quote: dict, item: dict, today: date) -> None:
    if quote.get("reference_type") != "QUOTE":
        raise AppError(409, "not_quote", "대안으로는 QUOTE만 선택할 수 있습니다.")
    price = number(quote.get("unit_price"))
    if price is None or price < 0:
        raise AppError(409, "quote_without_price", "유효한 단가가 없는 견적은 선택할 수 없습니다.")
    for key in ("item_name", "specification", "processing"):
        raw = requested.get(key)
        if not raw:
            raise AppError(409, "missing_spec", f"{key} 추출값을 먼저 확인해 주세요.")
        if raw.strip().casefold() != str(item.get(key) or "").strip().casefold():
            raise AppError(409, "spec_mismatch", f"선택한 견적의 {key}이 요청과 다릅니다.")
    for key in ("origin", "grade"):
        raw = requested.get(key)
        if raw and raw.strip().casefold() != str(item.get(key) or "").strip().casefold():
            raise AppError(409, "spec_mismatch", f"선택한 견적의 {key}이 요청과 다릅니다.")
    effective_raw = requested.get("effective_date")
    effective = iso_date(effective_raw) if effective_raw else today
    if effective is None:
        raise AppError(409, "invalid_date", "적용일은 YYYY-MM-DD로 확인해 주세요.")
    if quote.get("valid_until") and date.fromisoformat(quote["valid_until"]) < effective:
        raise AppError(409, "expired_quote", "선택한 견적의 유효기간이 적용일보다 이전입니다.")
    if date.fromisoformat(quote["reference_date"]) > effective:
        raise AppError(409, "future_quote", "적용일 이후의 견적입니다.")
    moq = number(quote.get("moq"))
    quantity = number(requested.get("requested_quantity"))
    if moq is not None and (quantity is None or quantity < moq):
        raise AppError(409, "moq_not_met", "요청 수량이 없거나 최소 주문 수량에 미달합니다.")
    delivery_raw = requested.get("requested_delivery_date")
    delivery = iso_date(delivery_raw)
    if delivery_raw and delivery is None:
        raise AppError(409, "invalid_delivery", "납기일은 YYYY-MM-DD로 확인해 주세요.")
    if quote.get("lead_days") is not None and delivery is not None:
        if (delivery - today).days < quote["lead_days"]:
            raise AppError(
                409, "lead_time", "선택한 견적의 리드타임으로는 납기를 맞출 수 없습니다."
            )
