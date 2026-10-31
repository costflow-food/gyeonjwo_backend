"""에이전트가 아닌 그래프 노드 (07 2·4절). 결정 가능한 검증은 Python·룰로 하고 LLM 검증 에이전트로 부르지 않는다.

각 노드는 CaseState를 받아 바꿀 필드만 dict로 돌려준다. graph.build_graph(nodes=Nodes(...))로 교체·주입한다.
계산은 app.features.review.domain.calculations, 숫자·단위·원문 검증은 app.features.review.domain.validation,
문서 읽기는 app.features.review.infrastructure.documents를 재사용한다.
"""
from dataclasses import dataclass
from typing import Callable


def preprocess(state) -> dict:
    """본문·PDF·스캔 OCR·표·Excel → 텍스트·페이지/셀 위치. 청킹은 내부 처리. 반환 예: {} (결과는 DB에 저장)."""
    raise NotImplementedError


def plan_validation(state) -> dict:
    """④ 구매안의 총액·공급량·규격·납기·MOQ·분산 정책 검증.
    반환: {'plan_check': PlanCheck, 'plan_check_reason': str | None, 'plans': 검증된 안 (PASS일 때),
           'pending_questions': ③에 넘길 미확인 (NEED_INFO일 때)}"""
    raise NotImplementedError


def price_validation(state) -> dict:
    """⑤ 역제안의 회사 가격 정책·근거 검증.
    state['last_action']이 COUNTER_OFFER_SKIPPED면 counter_offer는 None이다. 이때는 회사 정책이 역제안 생략을
    허용하는지 확인한다 (사유: state['counter_offer_skip_reason']).
    PASS → 검토 카드, FIX(정책 범위 밖 목표 조건·허용되지 않는 생략) → ⑤, NEED_INFO(근거·정책 없음) → ③.
    반환: {'price_check': PriceCheck, 'price_check_reason': ⑤에 넘길 사유 (FIX일 때),
           'pending_questions': ③에 넘길 미확인 (NEED_INFO일 때)}"""
    raise NotImplementedError


def review_card(state) -> dict:
    """검토 카드 저장. revision은 그래프가 직전에 확인한다. 역제안을 생략했으면 counter_offer_skip_reason을 표시한다.
    반환: {'card_id': str}"""
    raise NotImplementedError


@dataclass(frozen=True)
class Nodes:
    preprocess: Callable[[dict], dict] = preprocess
    plan_validation: Callable[[dict], dict] = plan_validation
    price_validation: Callable[[dict], dict] = price_validation
    review_card: Callable[[dict], dict] = review_card
