"""Verified calculation -> replaceable model -> semantic draft gate -> review draft."""
from __future__ import annotations

from copy import deepcopy

from .agent import prepare_counter_offer
from .contract import ReviewError
from .draft_contract import build_draft_request
from .draft_validation import validate_and_render_draft
from .model_adapter import DraftModel, ModelInfo
from .provider_errors import ProviderFailure


def _call_failed(result: dict, code: str, message: str) -> dict:
    result.update(outcome="FAILED", decision="DRAFT_UNAVAILABLE", draft=None,
                  draft_status="MODEL_CALL_FAILED", failure_stage="MODEL_CALL",
                  issues=[{"code": code, "field_name": "model", "message": message}])
    return result


def _provider_record(result: dict, model) -> None:
    record = getattr(model, "last_call", None)
    if not isinstance(record, dict) or not record:
        return
    result["execution"]["provider_call"] = deepcopy(record)
    simulated = record.get("transport_mode") == "SIMULATED_HTTP"
    result["execution"]["llm_used"] = (False if simulated or not record.get("inference_attempted")
                                      else True if record.get("model_response_received") else None)


def run_counter_offer_with_model(data: dict, model: DraftModel | None = None, *, model_factory=None) -> dict:
    """No automatic provider selection or silent fallback to the template path.

    An injected adapter is trusted application code. Its model output is untrusted.
    Protect the authoritative fact packet from mutation by passing a separate copy.
    """
    original = deepcopy(data)
    result = prepare_counter_offer(original)
    result.update(draft=None, draft_validation=None, draft_status="NOT_ATTEMPTED", failure_stage=None,
                  send_authorized=False, draft_input_summary=None)
    result["execution"] = {"mode": "MODEL_DRAFT_PIPELINE", "llm_used": False, "mail_sent": False,
                           "model_call_attempted": False, "model_mode": None, "model_id": None}
    if result["outcome"] != "SUCCESS":
        result["failure_stage"] = "INPUT_OR_CALCULATION"
        return result
    if result["decision"] == "NO_COUNTER_NEEDED":
        result["draft_status"] = "NOT_NEEDED"
        return result
    calculation = {key: result[key] for key in ("outcome", "decision", "offer", "issues", "input_digest")}
    try:
        packet = build_draft_request(original, calculation)
    except ReviewError as exc:
        result.update(outcome="REVIEW_REQUIRED", decision="REVIEW_REQUIRED", offer=None,
                      failure_stage="INPUT_OR_CALCULATION", issues=[exc.issue])
        return result
    return _generate_validated_draft(result, packet, model, model_factory=model_factory)


def _generate_validated_draft(result, packet, model=None, *, model_factory=None):
    """Shared model/error/semantic-validation path. Packet must come from a validating builder."""
    result["draft_input_summary"] = {"packet_id": packet["packet_id"], "schema_version": packet["schema_version"],
                                     "price_meaning": packet["verified_facts"]["proposal"]["price_meaning"],
                                     "quantity_scope": "CHANGEABLE_ONLY", "free_text_allowed": False}
    if model is None and model_factory is not None:
        try:
            model = model_factory()  # Delayed until evidence and calculation pass.
        except ProviderFailure as exc:
            return _call_failed(result, exc.code, exc.safe_message)
    if model is None:
        return _call_failed(result, "MODEL_NOT_CONFIGURED", "작성 모델 어댑터가 설정되지 않았습니다. 협상 가능 여부와 별개인 연결 상태입니다.")
    info = getattr(model, "info", None)
    if (not isinstance(info, ModelInfo) or info.mode not in ("MOCK", "PROVIDER")
            or type(info.model_id) is not str or not info.model_id.strip()):
        return _call_failed(result, "MODEL_CONFIGURATION_ERROR", "어댑터의 실행 유형과 모델 식별자가 필요합니다.")
    execution = result["execution"]
    execution.update(model_mode=info.mode, model_id=info.model_id, model_call_attempted=True,
                     llm_used=False if info.mode == "MOCK" else None)
    try:
        raw_response = model.generate(deepcopy(packet))
    except ProviderFailure as exc:
        _provider_record(result, model)
        _call_failed(result, exc.code, exc.safe_message)
        result["failure_stage"] = exc.stage
        if exc.stage != "MODEL_CALL":
            result["decision"] = "DRAFT_REJECTED"
            result["draft_status"] = "RESPONSE_FORMAT_ERROR" if exc.stage == "MODEL_RESPONSE_FORMAT" else "PROVIDER_RESPONSE_REJECTED"
        return result
    except Exception:
        # Provider errors may contain credentials or raw mail. Do not expose them.
        _provider_record(result, model)
        return _call_failed(result, "MODEL_CALL_FAILED", "초안 작성 호출이 실패했습니다. 검증된 계산은 유지되며 협상 불가 판정이 아닙니다.")
    execution["llm_used"] = info.mode == "PROVIDER"
    _provider_record(result, model)
    mode = "MOCK_MODEL_STRUCTURED" if info.mode == "MOCK" else "PROVIDER_MODEL_STRUCTURED"
    checked, draft = validate_and_render_draft(packet, raw_response, mode)
    result["draft_validation"] = checked
    if "provider_call" in execution:
        execution["provider_call"]["draft_validation_passed"] = checked["valid"]
    if not checked["valid"]:
        is_format_error = checked["failure_stage"] == "MODEL_RESPONSE_FORMAT"
        result.update(outcome="FAILED" if is_format_error else "REVIEW_REQUIRED", decision="DRAFT_REJECTED",
                      draft_status="RESPONSE_FORMAT_ERROR" if is_format_error else "GROUNDING_VIOLATION",
                      failure_stage=checked["failure_stage"], issues=checked["issues"])
        return result
    result.update(draft=draft, draft_status="VALIDATED_FOR_REVIEW")
    return result
