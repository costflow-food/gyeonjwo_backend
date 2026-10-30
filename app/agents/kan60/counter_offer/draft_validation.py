"""Strict semantic bindings plus closed wording. No free-prose safety claims."""
from __future__ import annotations

from copy import deepcopy
import json

from .contract import digest
from .draft_contract import DRAFT_SCHEMA


class DraftError(ValueError):
    def __init__(self, stage: str, code: str, field: str, message: str):
        super().__init__(message)
        self.stage = stage
        self.issue = {"code": code, "field_name": field, "message": message}


def _format(field: str, message: str) -> None:
    raise DraftError("MODEL_RESPONSE_FORMAT", "MODEL_RESPONSE_FORMAT_ERROR", field, message)


def _violation(field: str, message: str, code: str = "DRAFT_GROUNDING_VIOLATION") -> None:
    raise DraftError("DRAFT_GROUNDING", code, field, message)


def _keys(value, required: set[str], path: str) -> None:
    if type(value) is not dict:
        _format(path, "JSON 객체가 필요합니다.")
    if required - value.keys():
        _format(path, "필수 초안 필드가 누락되었습니다.")
    if value.keys() - required:
        _violation(path, "입력 계약에 없는 조건·자유문장·필드는 허용하지 않습니다.", "UNSUPPORTED_DRAFT_CONTENT")


def _same(actual, expected, path: str) -> None:
    # Strict type checks also reject bool-as-number and loosely typed JSON values.
    if type(actual) is not type(expected):
        # A concrete date where None was required is an invented commitment.
        if expected is None:
            _violation(path, "근거가 없는 날짜·유효기한·약속을 추가했습니다.")
        _format(path, "초안 필드의 자료형이 계약과 다릅니다.")
    if isinstance(expected, dict):
        _keys(actual, set(expected), path)
        for key in expected:
            _same(actual[key], expected[key], path + "." + key)
    elif isinstance(expected, list):
        if len(actual) != len(expected):
            _violation(path, "검증된 근거 식별자와 일치하지 않습니다.")
        for i, (value, wanted) in enumerate(zip(actual, expected)):
            _same(value, wanted, f'{path}[{i}]')
    elif actual != expected:
        _violation(path, "값 또는 가격의 의미·물량 범위·세금·비교 기준·조건이 검증된 사실과 다릅니다.")


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            _format("response", "중복 JSON 키는 허용하지 않습니다.")
        result[key] = value
    return result


def _invalid_constant(value):
    _format("response", "NaN·Infinity는 JSON 계약에서 허용하지 않습니다.")


def _check(packet: dict, raw_response: str) -> dict:
    if type(raw_response) is not str or len(raw_response) > 100_000:
        _format("response", "100,000자 이하의 JSON 응답 문자열이 필요합니다.")
    try:
        response = json.loads(raw_response, object_pairs_hook=_unique, parse_constant=_invalid_constant)
    except (json.JSONDecodeError, RecursionError) as exc:
        raise DraftError("MODEL_RESPONSE_FORMAT", "MODEL_RESPONSE_FORMAT_ERROR", "response", "올바른 단일 JSON 객체 응답이 필요합니다.") from exc
    _keys(response, {"schema_version", "packet_id", "assertions", "evidence_refs", "composition"}, "response")
    if response["schema_version"] != DRAFT_SCHEMA:
        _format("schema_version", "지원하지 않는 초안 계약입니다.")
    _same(response["packet_id"], packet["packet_id"], "packet_id")
    _same(response["assertions"], packet["verified_facts"], "assertions")
    _same(response["evidence_refs"], packet["evidence_refs"], "evidence_refs")
    composition = response["composition"]
    _keys(composition, {"subject_id", "opening_id", "explanation_ids", "closing_id"}, "composition")
    for field, group in (("subject_id", "subject"), ("opening_id", "opening"), ("closing_id", "closing")):
        if type(composition[field]) is not str:
            _format("composition." + field, "허용 문구 ID 문자열이 필요합니다.")
        if composition[field] not in packet["approved_wording"][group]:
            _violation("composition." + field, "승인되지 않은 문구·주장입니다.", "UNAPPROVED_WORDING")
    ids = composition["explanation_ids"]
    if type(ids) is not list or any(type(key) is not str for key in ids):
        _format("composition.explanation_ids", "설명 ID 문자열 배열이 필요합니다.")
    if (not 1 <= len(ids) <= 3 or len(set(ids)) != len(ids) or "PRICE_MEANING" not in ids
            or any(key not in packet["approved_wording"]["explanations"] for key in ids)):
        _violation("composition.explanation_ids", "가격 의미 설명은 필수이며 승인된 설명만 중복 없이 선택할 수 있습니다.", "UNAPPROVED_WORDING")
    return response


def validate_draft_response(packet: dict, raw_response: str) -> dict:
    """The packet must be an authoritative snapshot from build_draft_request."""
    try:
        response = _check(packet, raw_response)
        return {"valid": True, "failure_stage": None, "issues": [],
                "validation_ref": "KAN60-DRAFT-VALID-" + digest({"packet_id": packet["packet_id"], "response": response}),
                "structured_draft": deepcopy(response), "free_text_validated": False,
                "validation_scope": "TYPED_FACT_BINDINGS_AND_APPROVED_WORDING_ONLY"}
    except DraftError as exc:
        return {"valid": False, "failure_stage": exc.stage, "issues": [exc.issue],
                "validation_ref": None, "structured_draft": None, "free_text_validated": False,
                "validation_scope": "TYPED_FACT_BINDINGS_AND_APPROVED_WORDING_ONLY"}


def validate_and_render_draft(packet: dict, raw_response: str, mode: str) -> tuple[dict, dict | None]:
    """Always validate here; there is no public render-unchecked-response function."""
    checked = validate_draft_response(packet, raw_response)
    if not checked["valid"]:
        return checked, None
    # Render authoritative facts, never arbitrary strings from model prose.
    f = packet["verified_facts"]
    composition = checked["structured_draft"]["composition"]
    wording = packet["approved_wording"]
    p, fixed, costs = f["proposal"], f["fixed_commitment"], f["costs"]
    extra, terms = costs["buyer_extra_costs"], f["terms"]
    currency, unit = f["currency"], f["unit"]
    tax = {"INCLUDED": "세금 포함", "EXCLUDED": "세금 별도", "EXEMPT": "면세"}[f["tax_basis"]]
    prefix = "[합성 데이터 시연 · 발송 금지]" if packet["data_kind"] == "SYNTHETIC" else "[검토용 초안]"
    lines = [prefix, f"{f['supplier_id']} 담당자님께,", "", wording["opening"][composition["opening_id"]],
             f"품목: {f['item']['name']} / 규격: {f['item']['specification']}",
             f"{p['application_period']['from']}~{p['application_period']['to']}의 변경 가능 물량 "
             f"{p['quantity']}{unit}에 {p['unit_price']} {currency}/{unit} ({tax})를 제안드립니다.",
             f"고정 물량 {fixed['quantity']}{unit}에는 기존 계약 단가 {fixed['unit_price']} {currency}/{unit} ({tax})를 유지합니다.",
             f"납품 장소: {terms['delivery_location']}", f"납품 조건: {terms['delivery_terms']}",
             f"결제 조건: {terms['payment_terms']}",
             f"{f['comparison_period']['from']}~{f['comparison_period']['to']} 전체 비교기간의 구매사 추가비용은 "
             f"운송비 {extra['transport']} {currency}, 기타 물류비 {extra['other_logistics']} {currency}, "
             f"전환비용 {extra['switching']} {currency}입니다 ({tax}).",
             f"고정 물량과 변경 물량을 합친 총비용은 {costs['total_cost']} {currency}입니다 ({tax})."]
    lines.extend(wording["explanations"][key] for key in composition["explanation_ids"])
    lines.extend([wording["closing"][composition["closing_id"]], "본 내용은 협의 제안이며 공급처의 수락이나 주문 확정을 뜻하지 않습니다."])
    draft = {"subject": wording["subject"][composition["subject_id"]], "body": "\n".join(lines),
             "mode": mode, "delivery_status": "DRAFT_ONLY", "send_authorized": False,
             "validation_ref": packet["calculation_validation_ref"], "draft_validation_ref": checked["validation_ref"],
             "structured": deepcopy(checked["structured_draft"])}
    return checked, draft
