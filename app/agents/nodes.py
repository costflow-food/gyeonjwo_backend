"""에이전트가 아닌 그래프 노드 (07 2·4절). 결정 가능한 검증은 Python·룰로 하고 LLM 검증 에이전트로 부르지 않는다.

각 노드는 CaseState를 받아 바꿀 필드만 dict로 돌려준다. graph.build_graph(nodes=Nodes(...))로 교체·주입한다.
구매안·가격 검증은 app.agents.tools의 공통 계산(plan_total 등)을 에이전트 도구와 함께 쓴다(같은 숫자가 나온다).
문서 읽기는 app.features.review.infrastructure.documents를 재사용한다.
"""
import json
from dataclasses import dataclass
from typing import Callable, Protocol

from app.agents import tools
from app.agents.schemas import CounterOfferSummary, PlanCheck, PriceCheck, ReviewCard


class ReviewCardStore(Protocol):
    """검토 카드 저장소 (06 work_cases.latest_review_jsonb·latest_review_run_ids, workflow_logs REVIEW_CREATED)."""

    def save_review_card(self, case_id: str, expected_input_revision: int, card: dict) -> str | None:
        """업무의 최신 카드로 저장하고 상태를 Complete로 바꾼다. 이전 카드는 workflow_logs에 남긴다.
        revision 확인과 저장은 한 트랜잭션이다. 저장하면 card_id, revision이 바뀌었으면 None."""


_card_store: ReviewCardStore | None = None


def use_card_store(store: ReviewCardStore | None) -> None:
    global _card_store
    _card_store = store


def preprocess(state) -> dict:
    """본문·PDF·스캔 OCR·표·Excel → 텍스트·페이지/셀 위치. 청킹은 내부 처리. 반환 예: {} (결과는 DB에 저장)."""
    raise NotImplementedError


def plan_validation(state) -> dict:
    """④ 구매안을 최신 견적으로 다시 계산해 총액·공급량·MOQ·발주 배수·분산 정책·세금 기준을 검증한다.
    반환: {'plan_check': PlanCheck, 'plan_check_reason': 위반 목록 JSON (FIX·NEED_INFO일 때),
           'pending_questions': ③에 넘길 미확인 (NEED_INFO일 때)}"""
    plans = [plan.model_dump(mode='json') if hasattr(plan, 'model_dump') else plan for plan in state.get('plans', [])]
    result = tools.revalidate_plans(tools._priority_store, state['case_id'], plans)
    check = PlanCheck(result['check'])
    reason = json.dumps(result['violations'], ensure_ascii=False) if result['violations'] else None
    update = {'plan_check': check, 'plan_check_reason': reason}
    if check == PlanCheck.NEED_INFO:
        update['pending_questions'] = ['구매안의 견적이 최신이 아니다. 최신 견적으로 다시 확인한다.']
    return update


def price_validation(state) -> dict:
    """⑤ 역제안의 회사 가격 정책·근거 검증.
    state['last_action']이 COUNTER_OFFER_SKIPPED면 counter_offer는 None이다. 이때는 회사 정책이 역제안 생략을
    허용하는지 확인한다 (사유: state['counter_offer_skip_reason']).
    PASS → 검토 카드, FIX(정책 범위 밖 목표 조건·허용되지 않는 생략) → ⑤, NEED_INFO(근거·정책 없음) → ③.
    반환: {'price_check': PriceCheck, 'price_check_reason': ⑤에 넘길 사유 (FIX일 때),
           'pending_questions': ③에 넘길 미확인 (NEED_INFO일 때)}"""
    target = state.get('counter_offer')
    target = target.model_dump(mode='json') if hasattr(target, 'model_dump') else target
    plans = [plan.model_dump(mode='json') if hasattr(plan, 'model_dump') else plan for plan in state.get('plans', [])]
    result = tools.revalidate_counter_offer(tools._priority_store, state['case_id'], state['input_revision'], target,
                                            state.get('last_action') == 'COUNTER_OFFER_SKIPPED', plans)
    check = PriceCheck(result['check'])
    update = {'price_check': check, 'price_check_reason': result['reason'] if check == PriceCheck.FIX else None}
    if check == PriceCheck.NEED_INFO:
        update['pending_questions'] = [f"역제안 근거를 다시 확인한다({result['reason']})."]
    return update


COUNTER_STATUS = {'COUNTER_OFFER_PROPOSED': 'PROPOSED', 'COUNTER_OFFER_SKIPPED': 'SKIPPED', 'HOLD': 'HOLD'}


def build_review_card(state) -> ReviewCard:
    """그래프 상태에서 검토 카드를 만든다. ⑤가 돌지 않았으면 역제안 칸은 비운다."""
    result = state.get('counter_offer_result') or {}
    counter = None
    if result.get('next_action') in COUNTER_STATUS:
        counter = CounterOfferSummary(status=COUNTER_STATUS[result['next_action']], target=state.get('counter_offer'),
                                      reference=result.get('reference'), draft_ids=result.get('draft_ids', []),
                                      reason=result.get('reason'))
    return ReviewCard(case_id=state['case_id'], input_revision=state['input_revision'],
                      quote_version_ids=state.get('quote_version_ids', []), plans=state.get('plans', []),
                      counter_offer=counter, questions=state.get('questions', []),
                      unresolved=list(dict.fromkeys(state.get('unresolved', []))), steps=state.get('steps', []))


def review_card(state) -> dict:
    """검토 카드 저장. revision은 그래프가 직전에 확인하고, 저장소가 저장 트랜잭션 안에서 다시 확인한다.
    역제안을 생략·보류했으면 그 사유와 참고 계산을 카드에 보인다.
    반환: {'card_id': str} 또는 revision이 바뀌어 저장하지 못했으면 {'card_id': None}"""
    if _card_store is None:
        raise RuntimeError('review card store is not configured')
    card = build_review_card(state).model_dump(mode='json')
    return {'card_id': _card_store.save_review_card(state['case_id'], state['input_revision'], card)}


@dataclass(frozen=True)
class Nodes:
    preprocess: Callable[[dict], dict] = preprocess
    plan_validation: Callable[[dict], dict] = plan_validation
    price_validation: Callable[[dict], dict] = price_validation
    review_card: Callable[[dict], dict] = review_card
