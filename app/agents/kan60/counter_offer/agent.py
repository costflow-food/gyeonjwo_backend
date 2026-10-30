"""Code workflow now; the team's Claude drafting adapter is not yet connected."""
from __future__ import annotations

from .engine import calculate_counter_offer, validate_counter_offer


def _template_draft(offer: dict, validation_ref: str, data_kind: str) -> dict:
    conditions = offer["allowed_conditions"]
    terms = conditions["terms"]
    tax = {"INCLUDED": "세금 포함", "EXCLUDED": "세금 별도", "EXEMPT": "면세"}[terms["tax_basis"]]
    prefix = "[합성 데이터 시연 · 발송 금지]\n" if data_kind == "SYNTHETIC" else "[검토용 초안]\n"
    body = (
        f"{prefix}{conditions['supplier_id']} 담당자님께,\n\n"
        f"{conditions['item']['name']} 공급단가 인상 요청을 검토했습니다. 기존 거래를 이어가기 위해 "
        f"{conditions['changeable_from']}~{conditions['changeable_to']}의 변경 가능 물량 "
        f"{conditions['changeable_quantity']}{offer['unit']}에 대해 "
        f"{offer['target_unit_price']} {offer['currency']}/{offer['unit']} ({tax})로 협의를 요청드립니다.\n"
        f"고정 물량 {conditions['fixed_quantity']}{offer['unit']}의 단가는 기존 계약 "
        f"{conditions['fixed_unit_price']} {offer['currency']}/{offer['unit']}를 유지하는 조건입니다.\n"
        f"납품 장소: {terms['delivery_location']}\n납품 조건: {terms['delivery_terms']}\n"
        f"결제 조건: {terms['payment_terms']}\n"
        f"{conditions['period']['from']}~{conditions['period']['to']} 전체 기간의 구매사 추가비용은 "
        f"운송비 {conditions['extra_costs']['transport']} {offer['currency']}, "
        f"기타 물류비 {conditions['extra_costs']['other_logistics']} {offer['currency']}, "
        f"전환비용 {conditions['extra_costs']['switching']} {offer['currency']} 조건입니다.\n"
        "위 조건으로 공급 가능하신지 회신 부탁드립니다. 본 내용은 협의 제안이며 주문 확정이 아닙니다."
    )
    return {"subject": f"[협의 요청] {conditions['item']['name']} 공급단가 및 거래조건",
            "body": body, "mode": "TEMPLATE_NO_LLM", "validation_ref": validation_ref,
            "delivery_status": "DRAFT_ONLY"}


def prepare_counter_offer(data: dict) -> dict:
    """Calculate and independently validate; do not create any draft yet."""
    result = calculate_counter_offer(data)
    result.update({"schema_version": "kan60.v0", "contract_revision": "review-1", "draft": None, "validation": None,
                   "execution": {"mode": "RULES_AND_TEMPLATE", "llm_used": False, "mail_sent": False},
                   "request_id": data.get("request_id") if isinstance(data, dict) else None,
                   "data_kind": data.get("data_kind") if isinstance(data, dict) else None})
    if result["outcome"] != "SUCCESS":
        return result
    calculation = {key: result[key] for key in ("outcome", "decision", "offer", "issues", "input_digest")}
    validation = validate_counter_offer(data, calculation)
    result["validation"] = validation
    if not validation["valid"]:
        result.update({"outcome": "REVIEW_REQUIRED", "decision": validation["decision"],
                       "offer": None, "issues": validation["issues"]})
        return result
    return result


def run_counter_offer(data: dict) -> dict:
    """Original rules + template path, retained without changing price calculations."""
    result = prepare_counter_offer(data)
    if result["outcome"] == "SUCCESS" and result["decision"] == "READY":
        result["draft"] = _template_draft(result["offer"], result["validation"]["validation_ref"], data["data_kind"])
    return result
