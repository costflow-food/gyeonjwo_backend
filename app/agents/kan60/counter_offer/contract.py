"""Temporary kan60.v0 input contract. Decimal strings, explicit evidence only."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
import hashlib
import json
import re


class ReviewError(ValueError):
    def __init__(self, code: str, field: str, message: str, decision: str = "REVIEW_REQUIRED"):
        super().__init__(message)
        self.issue = {"code": code, "field_name": field, "message": message}
        self.decision = decision


def require(ok: bool, code: str, field: str, message: str, decision: str = "REVIEW_REQUIRED") -> None:
    if not ok:
        raise ReviewError(code, field, message, decision)


def shape(value, path: str, required: str, optional: str = "") -> dict:
    require(isinstance(value, dict), "INVALID_FIELD_VALUE", path, "JSON 객체가 필요합니다.")
    keys, allowed = set(required.split()), set((required + " " + optional).split())
    missing = keys - value.keys()
    require(not missing, "MISSING_REQUIRED_FIELD", path, f"필수 항목 누락: {', '.join(sorted(missing))}")
    require(not (value.keys() - allowed), "UNSUPPORTED_FIELD", path,
            f"지원하지 않는 항목: {', '.join(sorted(value.keys() - allowed))}. 계약 확장이 필요합니다.")
    return value


def string(value, path: str) -> str:
    require(isinstance(value, str) and bool(value.strip()) and len(value) <= 4000,
            "MISSING_REQUIRED_FIELD", path, "비어 있지 않은 문자열이 필요합니다.")
    return value


def number(value, path: str, *, positive: bool = False) -> Decimal:
    # Reject bool, float, NaN, infinity and exponent syntax. Bound precision explicitly.
    require(isinstance(value, str) and re.fullmatch(r"(?:0|[1-9][0-9]{0,14})(?:\.[0-9]{1,6})?", value) is not None,
            "INVALID_FIELD_VALUE", path, "0 이상의 십진수 문자열(정수 15자리·소수 6자리 이내)이 필요합니다.")
    result = Decimal(value)
    require(not positive or result > 0, "INVALID_FIELD_VALUE", path, "0보다 큰 값이 필요합니다.")
    return result


def day(value, path: str) -> date:
    require(isinstance(value, str) and re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value) is not None,
            "INVALID_FIELD_VALUE", path, "YYYY-MM-DD 날짜가 필요합니다.")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ReviewError("INVALID_FIELD_VALUE", path, "존재하지 않는 날짜입니다.") from exc


def money(value: Decimal) -> str:
    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def digest(data: dict) -> str:
    return hashlib.sha256(json.dumps(data, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()


def terms(value, path: str) -> dict:
    result = shape(value, path, "delivery_location delivery_terms payment_terms tax_basis")
    for key, val in result.items():
        string(val, f"{path}.{key}")
    require(result["tax_basis"] in ("INCLUDED", "EXCLUDED", "EXEMPT"), "INVALID_FIELD_VALUE",
            path + ".tax_basis", "세금 포함/별도/면세 기준을 명시해야 합니다.")
    return result


def item(value, path: str) -> dict:
    result = shape(value, path, "item_id name origin grade processing specification")
    for key, val in result.items():
        string(val, f"{path}.{key}")
    return result


def price_basis(value, d: dict, tax_basis: str, path: str) -> None:
    """Bind normalized source prices / policy limits to explicit units and specs."""
    basis = shape(value, path, "item_id specification unit currency tax_basis")
    expected = {"item_id": d["item"]["item_id"], "specification": d["item"]["specification"],
                "unit": d["unit"], "currency": d["currency"], "tax_basis": tax_basis}
    for key, wanted in expected.items():
        string(basis[key], f"{path}.{key}")
        require(basis[key] == wanted, "PRICE_BASIS_MISMATCH", f"{path}.{key}",
                "원문 가격·계약·정책 상한의 품목/규격/단위/통화/세금 기준이 비교 기준과 다릅니다.")


def comparison_context(value, d: dict, tax_basis: str, path: str) -> None:
    """An upstream cost calculation or budget must cover this exact scenario."""
    scope = shape(value, path, "period quantities fixed_unit_price item_id unit currency tax_basis")
    period = shape(scope["period"], path + ".period", "from to")
    for key in ("from", "to"):
        day(period[key], f"{path}.period.{key}")
        require(period[key] == d["period"][key], "COMPARISON_CONTEXT_MISMATCH", f"{path}.period.{key}",
                "비용/예산의 대상 기간이 비교기간과 다릅니다. 임의 배분하지 않고 재검증을 요청합니다.")
    quantities = shape(scope["quantities"], path + ".quantities", "fixed changeable")
    for key in ("fixed", "changeable"):
        require(number(quantities[key], f"{path}.quantities.{key}") == number(d["quantities"][key], f"quantities.{key}"),
                "COMPARISON_CONTEXT_MISMATCH", f"{path}.quantities.{key}", "비용/예산 검증 당시 물량과 현재 물량이 다릅니다.")
    require(number(scope["fixed_unit_price"], path + ".fixed_unit_price", positive=True)
            == number(d["contract"]["fixed_unit_price"], "contract.fixed_unit_price", positive=True),
            "COMPARISON_CONTEXT_MISMATCH", path + ".fixed_unit_price", "비용/예산 검증 당시 고정 단가가 다릅니다.")
    for key, wanted in (("item_id", d["item"]["item_id"]), ("unit", d["unit"]),
                        ("currency", d["currency"]), ("tax_basis", tax_basis)):
        string(scope[key], f"{path}.{key}")
        require(scope[key] == wanted, "COMPARISON_CONTEXT_MISMATCH", f"{path}.{key}", "비용/예산의 품목·단위·통화·세금 기준이 다릅니다.")


def costs(value: dict, path: str) -> Decimal:
    require(value["costs_confirmed"] is True, "COST_UNCONFIRMED", path + ".costs_confirmed",
            "비용을 확인해야 합니다. 미확인 비용을 0으로 채울 수 없습니다.")
    require(value["cost_scope"] == "FULL_PERIOD_BUYER_EXTRA", "COST_SCOPE_MISMATCH", path + ".cost_scope",
            "고정 물량까지 포함한 전체 비교기간의 구매사 추가비용으로 정규화해야 합니다.")
    string(value["cost_source_ref"], path + ".cost_source_ref")
    require(value["cost_model"] == "PRICE_INDEPENDENT", "UNSUPPORTED_COST_MODEL", path + ".cost_model",
            "가격에 따라 변하는 수수료·할인·부대비용은 지원하지 않습니다. 가격 독립 비용 근거가 필요합니다.")
    entries = shape(value["costs"], path + ".costs", "transport other_logistics switching")
    return sum((number(v, f"{path}.costs.{k}") for k, v in entries.items()), Decimal(0))


@dataclass(frozen=True)
class Context:
    raw: dict
    fixed: Decimal
    changeable: Decimal
    fixed_price: Decimal
    old_price: Decimal
    requested_price: Decimal
    current_extra: Decimal
    alternative_extra: Decimal
    alternative_price: Decimal
    alternative_total: Decimal
    max_price: Decimal
    max_total: Decimal
    increment: Decimal
    max_increase: Decimal | None
    explicit_target: Decimal | None

    @property
    def fixed_goods(self) -> Decimal:
        return self.fixed * self.fixed_price


def parse_input(data) -> Context:
    d = shape(data, "input", "schema_version contract_revision data_kind request_id as_of period item currency unit quantities notice contract alternative policy")
    require(d["schema_version"] == "kan60.v0", "UNSUPPORTED_SCHEMA", "schema_version", "kan60.v0 계약만 지원합니다.")
    require(d["contract_revision"] == "review-1", "UNSUPPORTED_SCHEMA", "contract_revision", "임시 kan60.v0의 review-1 필수 근거 계약이 필요합니다.")
    require(d["data_kind"] in ("SYNTHETIC", "LIVE"), "INVALID_FIELD_VALUE", "data_kind", "SYNTHETIC 또는 LIVE를 명시하세요.")
    string(d["request_id"], "request_id")
    as_of = day(d["as_of"], "as_of")
    period = shape(d["period"], "period", "from to")
    start, end = day(period["from"], "period.from"), day(period["to"], "period.to")
    require(as_of <= start <= end, "INVALID_COMPARISON_DATE", "period", "판단일 이후의 비교기간이 필요합니다.")
    item(d["item"], "item")
    require(d["currency"] == "KRW", "CURRENCY_NOT_SUPPORTED", "currency", "MVP는 KRW만 지원하며 환율을 추정하지 않습니다.")
    string(d["unit"], "unit")
    quantities = shape(d["quantities"], "quantities", "fixed changeable")
    fixed = number(quantities["fixed"], "quantities.fixed")
    changeable = number(quantities["changeable"], "quantities.changeable", positive=True)

    n = shape(d["notice"], "notice", "supplier_id old_unit_price requested_unit_price effective_date evidence")
    string(n["supplier_id"], "notice.supplier_id")
    old = number(n["old_unit_price"], "notice.old_unit_price", positive=True)
    requested = number(n["requested_unit_price"], "notice.requested_unit_price", positive=True)
    require(requested > old, "NOT_PRICE_INCREASE", "notice.requested_unit_price", "MVP는 단가 인상 요청만 처리합니다.")
    effective = day(n["effective_date"], "notice.effective_date")
    ev = shape(n["evidence"], "notice.evidence", "old_unit_price requested_unit_price effective_date")
    for key in ev:
        entry = shape(ev[key], f"notice.evidence.{key}",
                      "source_ref quote value verified" + (" basis" if key != "effective_date" else ""))
        string(entry["source_ref"], f"notice.evidence.{key}.source_ref")
        string(entry["quote"], f"notice.evidence.{key}.quote")
        require(entry["verified"] is True and entry["value"] == n[key], "SOURCE_EVIDENCE_UNVERIFIED",
                f"notice.evidence.{key}", "원문과 정규화 값의 일치를 상위 추출·검증 단계에서 확인해야 합니다.")

    c = shape(d["contract"], "contract",
              "source_ref verification_ref verified supplier_id fixed_unit_price changeable_from changeable_to costs costs_confirmed cost_scope cost_source_ref cost_model cost_context price_basis terms")
    for key in ("source_ref", "verification_ref", "supplier_id"):
        string(c[key], f"contract.{key}")
    require(c["verified"] is True, "CONTRACT_UNVERIFIED", "contract.verified", "기존 계약 검증이 필요합니다.")
    require(c["supplier_id"] == n["supplier_id"], "SUPPLIER_MISMATCH", "contract.supplier_id", "인상 요청과 계약의 공급처가 다릅니다.")
    change_start, change_end = day(c["changeable_from"], "contract.changeable_from"), day(c["changeable_to"], "contract.changeable_to")
    require(start <= change_start <= change_end <= end and effective <= change_start,
            "EFFECTIVE_DATE_MISMATCH", "contract.changeable_from", "변경 가능 물량은 비교기간 내이면서 인상 적용일 이후여야 합니다.")
    fixed_price = number(c["fixed_unit_price"], "contract.fixed_unit_price", positive=True)
    current_extra = costs(c, "contract")
    require(number(c["costs"]["switching"], "contract.costs.switching") == 0,
            "INVALID_FIELD_VALUE", "contract.costs.switching", "기존 거래 유지 시 전환비용은 확인된 0이어야 합니다.")
    current_terms = terms(c["terms"], "contract.terms")
    tax_basis = current_terms["tax_basis"]
    price_basis(c["price_basis"], d, tax_basis, "contract.price_basis")
    for key in ("old_unit_price", "requested_unit_price"):
        price_basis(ev[key]["basis"], d, tax_basis, f"notice.evidence.{key}.basis")
    comparison_context(c["cost_context"], d, tax_basis, "contract.cost_context")

    if isinstance(d["alternative"], dict) and d["alternative"].get("reference_type") == "REPRICED_SPLIT_PLAN":
        raise ReviewError("UNSUPPORTED_COST_MODEL", "alternative.reference_type",
                          "기존 거래처 물량이 제안 단가로 재가격되는 분산안은 단일·가격 독립 대안으로 평가할 수 없습니다.", "HOLD")
    a = shape(d["alternative"], "alternative",
              "reference_type source_ref verification_ref stage4_status verified_on comparison_as_of supplier_id valid_from valid_until supply_from supply_to item currency unit quantity moq available_quantity lead_days unit_price costs costs_confirmed cost_scope cost_source_ref cost_model cost_context total_cost terms")
    require(a["reference_type"] == "CURRENT_QUOTE", "NOT_CURRENT_ALTERNATIVE", "alternative.reference_type",
            "과거 구매가격은 현행 대안이 아닙니다. 현재 구매 가능한 견적이 필요합니다.")
    require(a["stage4_status"] == "VERIFIED", "ALTERNATIVE_UNVERIFIED", "alternative.stage4_status", "④ 단계 비용·조건 검증이 필요합니다.")
    require(day(a["comparison_as_of"], "alternative.comparison_as_of") == as_of,
            "COMPARISON_DATE_MISMATCH", "alternative.comparison_as_of",
            "④ 검증의 비교 기준일이 현재 판단일과 다릅니다. 해당 기준일로 재검증해야 합니다.")
    for key in ("source_ref", "verification_ref", "supplier_id"):
        string(a[key], f"alternative.{key}")
    require(a["supplier_id"] != n["supplier_id"], "NOT_CURRENT_ALTERNATIVE", "alternative.supplier_id", "현재 공급처와 구별되는 대안이 필요합니다.")
    valid_from, valid_until = day(a["valid_from"], "alternative.valid_from"), day(a["valid_until"], "alternative.valid_until")
    verified_on = day(a["verified_on"], "alternative.verified_on")
    require(valid_from <= verified_on <= as_of <= valid_until, "QUOTE_NOT_CURRENT", "alternative.valid_until",
            "판단일에 유효하며 미래가 아닌 시점에 검증된 견적이 필요합니다.")
    supply_from, supply_to = day(a["supply_from"], "alternative.supply_from"), day(a["supply_to"], "alternative.supply_to")
    require(supply_from <= change_start <= change_end <= supply_to, "SUPPLY_PERIOD_MISMATCH", "alternative.supply_from",
            "대안 견적의 공급기간이 변경 가능 물량의 납기를 포함해야 합니다.")
    lead = a["lead_days"]
    require(type(lead) is int and 0 <= lead <= 36500, "INVALID_FIELD_VALUE", "alternative.lead_days", "납기는 0~36500 사이 정수 일수여야 합니다.")
    require((change_start - as_of).days >= lead, "LEAD_TIME_MISMATCH", "alternative.lead_days", "첫 변경 물량 납기를 맞출 수 없습니다.")
    require(item(a["item"], "alternative.item") == d["item"], "ITEM_SPEC_MISMATCH", "alternative.item", "품목·원산지·등급·가공상태가 일치해야 합니다.")
    require(a["currency"] == d["currency"] and a["unit"] == d["unit"], "COMPARISON_BASIS_MISMATCH", "alternative.unit", "통화·단위를 먼저 정규화해야 합니다.")
    aq = number(a["quantity"], "alternative.quantity", positive=True)
    moq = number(a["moq"], "alternative.moq")
    available = number(a["available_quantity"], "alternative.available_quantity", positive=True)
    require(aq == changeable and moq <= aq <= available, "QUANTITY_NOT_FEASIBLE", "alternative.quantity", "변경 물량과 견적 수량·MOQ·공급 가능 수량이 맞아야 합니다.")
    require(terms(a["terms"], "alternative.terms") == current_terms, "TRADE_TERMS_MISMATCH", "alternative.terms", "납품·결제·세금 조건이 다른 견적은 정규화 검증 후 비교하세요.")
    alternative_extra = costs(a, "alternative")
    comparison_context(a["cost_context"], d, tax_basis, "alternative.cost_context")
    alternative_price = number(a["unit_price"], "alternative.unit_price", positive=True)
    alternative_total = number(a["total_cost"], "alternative.total_cost", positive=True)
    require(alternative_total == fixed * fixed_price + changeable * alternative_price + alternative_extra,
            "ALTERNATIVE_TOTAL_MISMATCH", "alternative.total_cost", "④ 총비용과 고정 물량·변경 물량·추가비용 재계산이 다릅니다.")

    p = shape(d["policy"], "policy",
              "policy_id source_ref approved valid_from valid_to target_rule price_increment max_unit_price max_total_cost require_not_more_than_alternative retention_terms price_basis budget_context",
              "max_increase_rate explicit_target_price min_unit_price skip_if_request_not_more_than_alternative")
    string(p["policy_id"], "policy.policy_id")
    string(p["source_ref"], "policy.source_ref")
    require(p["approved"] is True, "POLICY_UNAPPROVED", "policy.approved", "승인된 회사 정책이 필요합니다.")
    p_from, p_to = day(p["valid_from"], "policy.valid_from"), day(p["valid_to"], "policy.valid_to")
    require(p_from <= as_of <= start <= end <= p_to, "POLICY_NOT_CURRENT", "policy.valid_to", "정책이 판단일과 비교기간 전체에 유효해야 합니다.")
    require(p["target_rule"] in ("EXPLICIT_TARGET", "MATCH_ALTERNATIVE_TOTAL_WITH_CAPS"), "UNSUPPORTED_POLICY", "policy.target_rule", "목표 산식을 회사 정책으로 명시해야 합니다.")
    require(type(p["require_not_more_than_alternative"]) is bool, "INVALID_FIELD_VALUE", "policy.require_not_more_than_alternative", "대안 총비용 상한 사용 여부를 명시하세요.")
    require(p["target_rule"] != "MATCH_ALTERNATIVE_TOTAL_WITH_CAPS" or p["require_not_more_than_alternative"] is True,
            "AMBIGUOUS_POLICY", "policy.require_not_more_than_alternative",
            "대안 총비용 기준 산식과 대안 상한 해제 설정이 충돌합니다. 정책을 확인하세요.")
    require(terms(p["retention_terms"], "policy.retention_terms") == current_terms, "RETENTION_POLICY_VIOLATION", "policy.retention_terms", "현재 거래조건이 회사의 유지 조건을 충족하지 않습니다.", "HOLD")
    price_basis(p["price_basis"], d, tax_basis, "policy.price_basis")
    comparison_context(p["budget_context"], d, tax_basis, "policy.budget_context")
    increment = number(p["price_increment"], "policy.price_increment", positive=True)
    max_price = number(p["max_unit_price"], "policy.max_unit_price", positive=True)
    if "min_unit_price" in p:
        minimum = number(p["min_unit_price"], "policy.min_unit_price")
        require(minimum <= max_price, "POLICY_VIOLATION", "policy.min_unit_price", "정책 하한이 상한을 초과합니다.", "HOLD")
    if "skip_if_request_not_more_than_alternative" in p:
        require(type(p["skip_if_request_not_more_than_alternative"]) is bool,
                "INVALID_FIELD_VALUE", "policy.skip_if_request_not_more_than_alternative", "명시적인 bool 정책이 필요합니다.")
        require(not p["skip_if_request_not_more_than_alternative"] or p["target_rule"] == "MATCH_ALTERNATIVE_TOTAL_WITH_CAPS",
                "AMBIGUOUS_POLICY", "policy.skip_if_request_not_more_than_alternative", "명시 목표가와 역제안 생략 규칙을 동시에 사용할 수 없습니다.")
    max_total = number(p["max_total_cost"], "policy.max_total_cost", positive=True)
    max_increase = None if p.get("max_increase_rate") is None else number(p["max_increase_rate"], "policy.max_increase_rate")
    explicit = None
    if p["target_rule"] == "EXPLICIT_TARGET":
        explicit = number(p.get("explicit_target_price"), "policy.explicit_target_price", positive=True)
    else:
        require(p.get("explicit_target_price") is None, "AMBIGUOUS_POLICY", "policy.explicit_target_price", "산식과 명시적 목표를 동시에 적용할 수 없습니다.")
    return Context(d, fixed, changeable, fixed_price, old, requested, current_extra,
                   alternative_extra, alternative_price, alternative_total, max_price,
                   max_total, increment, max_increase, explicit)
