"""Calculation and independent checks. No network, SDK, model, or mail access."""
from __future__ import annotations

from copy import deepcopy
from decimal import Decimal, ROUND_FLOOR, localcontext

from .contract import Context, ReviewError, digest, money, number, parse_input, require, shape
from .pricing import select_price


def references(c: Context) -> list[str]:
    d = c.raw
    refs = [entry["source_ref"] for entry in d["notice"]["evidence"].values()]
    for group in ("contract", "alternative"):
        refs.extend(d[group][key] for key in ("source_ref", "verification_ref", "cost_source_ref"))
    refs.append(d["policy"]["source_ref"])
    return sorted(set(refs))


def allowed_conditions(c: Context) -> dict:
    d = c.raw
    return deepcopy({
        "supplier_id": d["notice"]["supplier_id"], "item": d["item"],
        "period": d["period"], "terms": d["policy"]["retention_terms"],
        "changeable_from": d["contract"]["changeable_from"],
        "changeable_to": d["contract"]["changeable_to"],
        "fixed_quantity": money(c.fixed), "fixed_unit_price": money(c.fixed_price),
        "changeable_quantity": money(c.changeable),
        "cost_scope": d["contract"]["cost_scope"], "extra_costs": d["contract"]["costs"],
        "cost_model": d["contract"]["cost_model"], "cost_context": d["contract"]["cost_context"],
    })


def rationale(c: Context) -> list[str]:
    return [
        f"승인 정책 {c.raw['policy']['policy_id']}의 {c.raw['policy']['target_rule']} 규칙을 적용했습니다.",
        "고정 물량 금액 + 변경 가능 수량 × 목표 단가 + 전체 기간 구매사 추가비용으로 계산했습니다.",
        "현행 견적의 유효기간·공급기간·규격·수량·MOQ·납기·납품·결제·세금과 검증 기준일, 비용·예산 대상 범위를 대조했습니다.",
        "과거 단가는 인상 전 기준 및 명시된 인상률 상한 확인에만 사용했습니다.",
        "원문 내용의 의미·진위는 상위 단계의 검증 기록을 신뢰하며, 이 도구는 기록의 존재와 값 일치를 확인합니다.",
    ]


def blocked(error: ReviewError) -> dict:
    return {"outcome": "REVIEW_REQUIRED", "decision": error.decision, "offer": None,
            "issues": [error.issue], "input_digest": None}


def _calculate(c: Context) -> dict:
    p = c.raw["policy"]
    fixed_cost = c.fixed_goods + c.current_extra
    caps = [c.max_price, c.requested_price, (c.max_total - fixed_cost) / c.changeable]
    if c.max_increase is not None:
        caps.append(c.old_price * (1 + c.max_increase))
    if p["require_not_more_than_alternative"] or p["target_rule"] == "MATCH_ALTERNATIVE_TOTAL_WITH_CAPS":
        caps.append((c.alternative_total - fixed_cost) / c.changeable)
    ceiling = min(caps)
    minimum = Decimal(p.get("min_unit_price", "0"))
    if (p.get("skip_if_request_not_more_than_alternative") is True
            and minimum <= c.requested_price <= ceiling
            and c.requested_price % c.increment == 0
            and fixed_cost + c.changeable * c.requested_price <= c.alternative_total):
        return {"outcome": "SUCCESS", "decision": "NO_COUNTER_NEEDED", "offer": None,
                "issues": [], "input_digest": digest(c.raw)}
    target = select_price(ceiling, c.increment, minimum, c.explicit_target)
    goods = c.fixed_goods + c.changeable * target
    total = goods + c.current_extra
    before = c.fixed_goods + c.changeable * c.old_price + c.current_extra
    requested_total = c.fixed_goods + c.changeable * c.requested_price + c.current_extra
    offer = {
        "target_unit_price": money(target), "unit": c.raw["unit"], "currency": c.raw["currency"],
        "goods_amount": money(goods), "total_cost": money(total),
        "baseline_total_cost": money(before), "requested_total_cost": money(requested_total),
        "alternative_total_cost": money(c.alternative_total),
        "savings_vs_request": money(requested_total - total), "difference_from_before": money(total - before),
        "allowed_conditions": allowed_conditions(c), "rationale": rationale(c),
        "verification_refs": references(c),
    }
    return {"outcome": "SUCCESS", "decision": "READY", "offer": offer, "issues": [], "input_digest": digest(c.raw)}


def calculate_counter_offer(data: dict) -> dict:
    """Use only the caller's approved policy. Incomplete evidence returns no offer."""
    try:
        with localcontext() as ctx:
            ctx.prec = 80
            return _calculate(parse_input(data))
    except ReviewError as exc:
        return blocked(exc)


def validate_counter_offer(data: dict, calculation: dict) -> dict:
    """Independently recompute amounts and enforce policy before allowing a draft.

    This does not call calculate_counter_offer. Schema/evidence helpers are shared;
    arithmetic checks, constraints and maximal-price checks are separate.
    """
    try:
        with localcontext() as ctx:
            ctx.prec = 80
            c = parse_input(data)
            result = shape(calculation, "calculation", "outcome decision offer issues input_digest")
            if result["decision"] == "NO_COUNTER_NEEDED":
                policy = data["policy"]
                fixed = c.fixed * c.fixed_price
                requested = fixed + c.changeable * c.requested_price + sum(Decimal(v) for v in data["contract"]["costs"].values())
                alternative = fixed + c.changeable * c.alternative_price + sum(Decimal(v) for v in data["alternative"]["costs"].values())
                require(result["outcome"] == "SUCCESS" and result["offer"] is None and result["issues"] == []
                        and result["input_digest"] == digest(data), "CALCULATION_NOT_READY", "calculation", "초안 없는 검증 결과 형식/입력 연결이 다릅니다.")
                require(policy.get("skip_if_request_not_more_than_alternative") is True
                        and requested <= alternative and requested <= c.max_total
                        and Decimal(policy.get("min_unit_price", "0")) <= c.requested_price <= c.max_price
                        and c.requested_price % c.increment == 0
                        and (c.max_increase is None or c.requested_price <= c.old_price * (1 + c.max_increase)),
                        "NO_COUNTER_DECISION_MISMATCH", "calculation.decision", "역제안 불필요 판단이 비용/명시 정책과 일치하지 않습니다.")
                return {"valid": True, "issues": [], "decision": "NO_COUNTER_NEEDED",
                        "validation_ref": "KAN60-NO-COUNTER-" + digest({"input": data, "decision": "NO_COUNTER_NEEDED"})}
            require(result["outcome"] == "SUCCESS" and result["decision"] == "READY" and result["issues"] == [],
                    "CALCULATION_NOT_READY", "calculation", "정상 계산 결과가 필요합니다.")
            require(result["input_digest"] == digest(data), "INPUT_CHANGED", "input_digest", "계산 후 입력이 변경되었습니다. 재계산이 필요합니다.")
            offer = shape(result["offer"], "offer",
                          "target_unit_price unit currency goods_amount total_cost baseline_total_cost requested_total_cost alternative_total_cost savings_vs_request difference_from_before allowed_conditions rationale verification_refs")
            price = number(offer["target_unit_price"], "offer.target_unit_price", positive=True)
            require(price >= Decimal(data["policy"].get("min_unit_price", "0")), "POLICY_VIOLATION", "offer.target_unit_price", "목표가가 명시 정책 하한 미만입니다.", "HOLD")
            require(price % c.increment == 0, "PRICE_INCREMENT_VIOLATION", "offer.target_unit_price", "정책의 가격 증분을 위반했습니다.", "HOLD")
            require(price <= c.max_price and price <= c.requested_price, "POLICY_VIOLATION", "offer.target_unit_price", "목표가가 회사 상한 또는 인상 요청가를 초과했습니다.", "HOLD")
            fixed_goods = c.fixed * c.fixed_price
            goods = fixed_goods + c.changeable * price
            # Sum source components again rather than trust the proposal's totals.
            extra = sum((Decimal(v) for v in data["contract"]["costs"].values()), Decimal(0))
            total = goods + extra
            before = fixed_goods + c.changeable * c.old_price + extra
            requested = fixed_goods + c.changeable * c.requested_price + extra
            alternative = fixed_goods + c.changeable * c.alternative_price + sum(
                (Decimal(v) for v in data["alternative"]["costs"].values()), Decimal(0))
            require(total <= c.max_total, "POLICY_VIOLATION", "offer.total_cost", "회사 총비용 상한을 초과했습니다.", "HOLD")
            if c.max_increase is not None:
                require(price - c.old_price <= c.old_price * c.max_increase,
                        "POLICY_VIOLATION", "offer.target_unit_price", "명시된 허용 인상률을 초과했습니다.", "HOLD")
            policy = data["policy"]
            should_skip = (policy.get("skip_if_request_not_more_than_alternative") is True
                           and requested <= alternative and requested <= c.max_total
                           and Decimal(policy.get("min_unit_price", "0")) <= c.requested_price <= c.max_price
                           and c.requested_price % c.increment == 0
                           and (c.max_increase is None or c.requested_price <= c.old_price * (1 + c.max_increase)))
            require(not should_skip, "NO_COUNTER_DECISION_MISMATCH", "calculation.decision", "명시 정책상 역제안이 불필요한데 초안용 계산을 반환했습니다.")
            if policy["require_not_more_than_alternative"] or policy["target_rule"] == "MATCH_ALTERNATIVE_TOTAL_WITH_CAPS":
                require(total <= alternative, "POLICY_VIOLATION", "offer.total_cost", "정책이 요구하는 현행 대안 총비용 상한을 초과했습니다.", "HOLD")
            if policy["target_rule"] == "EXPLICIT_TARGET":
                require(price == c.explicit_target, "TARGET_RULE_MISMATCH", "offer.target_unit_price", "회사에서 명시한 목표가와 다릅니다.", "HOLD")
            else:
                # The next increment must violate at least one bound. This verifies
                # the chosen maximum feasible grid price without reusing min/division.
                next_price = price + c.increment
                next_total = fixed_goods + c.changeable * next_price + extra
                hits_limit = (next_price > c.max_price or next_price > c.requested_price
                              or next_total > c.max_total or next_total > alternative
                              or (c.max_increase is not None and next_price - c.old_price > c.old_price * c.max_increase))
                require(hits_limit, "TARGET_RULE_MISMATCH", "offer.target_unit_price", "정책 산식으로 정한 목표가와 다릅니다.", "HOLD")
            amounts = {"goods_amount": goods, "total_cost": total, "baseline_total_cost": before,
                       "requested_total_cost": requested, "alternative_total_cost": alternative,
                       "savings_vs_request": requested - total, "difference_from_before": total - before}
            for key, value in amounts.items():
                require(offer[key] == money(value), "CALCULATION_MISMATCH", "offer." + key, "독립 재계산과 금액이 일치하지 않습니다.", "HOLD")
            expected = {"unit": data["unit"], "currency": data["currency"], "allowed_conditions": allowed_conditions(c),
                        "rationale": rationale(c), "verification_refs": references(c)}
            for key, value in expected.items():
                require(offer[key] == value, "OUTPUT_EVIDENCE_MISMATCH", "offer." + key, "입력 근거·조건과 출력이 일치하지 않습니다.", "HOLD")
            validation_ref = "KAN60-" + digest({"input": data, "offer": offer})
            return {"valid": True, "issues": [], "decision": "READY", "validation_ref": validation_ref}
    except ReviewError as exc:
        return {"valid": False, "issues": [exc.issue], "decision": exc.decision, "validation_ref": None}
