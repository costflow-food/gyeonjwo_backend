"""에이전트 도구 (07 4절). 도구 구현은 담당 팀원이 @register로 등록하고, 역할별 허용 목록은 ROLE_TOOLS가 정한다.

에이전트는 허용 목록에 있는 등록된 도구만 받는다. Gmail 자격증명·임의 SQL·무제한 웹 탐색은 도구로 주지 않는다.
분류(classify_mail_input)·전처리(preprocess_mail_documents)·발송(dispatch_mail)은 노드·백엔드 몫이라 여기 없다.

도구 작성 규칙
- 이름은 ROLE_TOOLS에 있는 이름 그대로 쓴다. 목록에 없는 이름은 register가 거부한다 (추가는 오케스트레이션 담당과 협의).
- docstring이 LLM에게 보이는 도구 설명이다. 언제 쓰는지·입력·출력을 짧게 적는다.
- 인자는 LLM이 채운다. case_id 등 범위 값을 믿지 말고 소유·업무 범위·revision을 도구 안에서 검사한다.
- 결과는 근거 ID·값·미확인 목록처럼 작게 돌려준다. 원문 전체를 돌려주지 않는다.
- 금액·수량 계산은 LLM에게 맡기지 않고 도구가 Decimal로 계산한다.
  계산은 app.features.review.domain.calculations, 숫자·단위 검증은 app.features.review.domain.validation을 재사용한다.
- 예외를 올리면 그 에이전트 호출이 실패하고 그래프가 FAILED로 끝난다. 예상 가능한 결과(없음·미확인)는 값으로 돌려준다.

예시 — 이 파일 맨 아래 역할별 구역에 추가한다.

    from langchain_core.tools import tool

    @register
    @tool
    def read_mail_evidence(case_id: str, evidence_id: str) -> dict:
        '''업무 범위 안의 메일·첨부 원문 구간을 읽는다. 반환: {evidence_id, text, location}'''
        ...

도구별 입력 → 출력 (07 4절)
| 도구                      | 역할  | 입력 → 출력                                                     | 경계                              |
| get_case_context          | 공통  | case_id → 요청 조건·회사 정책·revision                          | 소유·업무 범위 제한               |
| read_mail_evidence        | 공통  | 근거 ID → 원문 구간·위치(페이지/셀)                             | 소유·업무 범위 제한               |
| save_agent_result         | 공통  | 역할 결과·근거·revision → 결과 ID·최신 여부                     | 입력 revision 확인                |
| store_structured_mail     | ①     | 필드·품목 비고·근거·expected_revision → 견적 버전 ID            | 서버 검증 후 저장, 임의 SQL 불가  |
| search_supplier_evidence  | ②     | 품목·규격·필수 조건 → 취급 근거·후보                            | 과거 단가를 현재 가격으로 안 줌   |
| crawl_supplier_candidates | ②     | 허용 출처·조건 → 잠정 후보·연락처·출처 시점 (Should)            | 잠정 후보, 회신으로 확인          |
| check_comparability       | ③④⑤   | 견적 버전·정책 → 누락·불일치·비교 범위                          | 필수·가중 조건 구분               |
| calculate_break_even      | ③     | 확인된 비용·제약 → 역전 기준 금액·산식                          | 미확인 금액을 0원으로 보지 않음   |
| build_plan_candidates     | ④     | 견적·정책 → 유지·전환·분산(최대 2곳) 안                          | 단가 구간·MOQ·납기·비용 중복 검사 |
| validate_purchase_plan    | ④     | 배분안 → 총액·위반 사유·탐색 범위                               | 위와 같음                         |
| calculate_counter_offer   | ⑤     | 기존·요청 가격·대안 총액·회사 정책 → 목표 조건별 비용            | 정책 없는 인상률 생성 금지        |
| validate_counter_offer    | ⑤     | 목표 조건 → 가격 범위·정책 검증 결과                            | 위와 같음                         |
| create_mail_draft         | ②③⑤   | 목적(RFQ/CLARIFICATION/COUNTER_OFFER)·대상·근거·스타일 → 초안 ID | 내부 저장만, 발송은 백엔드        |
"""
from langchain_core.tools import BaseTool
from langchain_core.tools import tool

from app.agents.schemas import AgentRole

_COMMON = ('get_case_context', 'read_mail_evidence', 'save_agent_result')

ROLE_TOOLS = {
    AgentRole.INFO_EXTRACTOR: _COMMON + ('store_structured_mail',),
    AgentRole.CANDIDATE: _COMMON + ('search_supplier_evidence', 'crawl_supplier_candidates', 'create_mail_draft'),
    AgentRole.INFO_PRIORITY: _COMMON + ('check_comparability', 'calculate_break_even', 'create_mail_draft'),
    AgentRole.RESPONSE_PLAN: _COMMON + ('check_comparability', 'build_plan_candidates', 'validate_purchase_plan'),
    AgentRole.COUNTER_OFFER: _COMMON + ('check_comparability', 'calculate_counter_offer', 'validate_counter_offer',
                                        'create_mail_draft'),
}
KNOWN_TOOLS = frozenset(name for names in ROLE_TOOLS.values() for name in names)

_registry: dict[str, BaseTool] = {}


def register(tool: BaseTool) -> BaseTool:
    if tool.name not in KNOWN_TOOLS:
        raise ValueError(f'{tool.name} is not in ROLE_TOOLS (07 4절)')
    if tool.name in _registry:
        raise ValueError(f'{tool.name} is already registered')
    _registry[tool.name] = tool
    return tool


def tools_for(role: AgentRole) -> list[BaseTool]:
    """역할에 허용되고 등록된 도구. 아직 구현되지 않은 도구는 빠진다."""
    return [_registry[name] for name in ROLE_TOOLS[role] if name in _registry]


# ─────────────────────────────────────────────
# 도구 구현 — 역할별 구역에만 추가한다 (구역을 나눠 병합 충돌을 줄인다)
# ─────────────────────────────────────────────

# 공통: get_case_context, read_mail_evidence, save_agent_result

@register
@tool
def get_case_context(case_id: str, expected_revision: int) -> dict:
    """소유·batch·revision을 검사하고 요청 조건과 실행 범위의 메일 ID를 조회한다."""
    from app.features.work_cases.application.extraction_tools import context
    return context(case_id, expected_revision)


@register
@tool
def read_mail_evidence(case_id: str, expected_revision: int, mail_id: str) -> dict:
    """실행 범위 메일의 본문·서명·PDF 텍스트를 읽고 evidence_id와 읽기 상태를 반환한다."""
    from app.features.work_cases.application.extraction_tools import evidence
    return evidence(case_id, expected_revision, mail_id)


@register
@tool
def save_agent_result(case_id: str, expected_revision: int, result: dict) -> dict:
    """① 추출 fields·unknown_fields·evidence를 검증해 일괄 저장할 결과로 준비한다. 아직 DB 저장은 아니다."""
    from app.features.work_cases.application.extraction_tools import stage
    return stage(case_id, expected_revision, result)

# ① INFO_EXTRACTOR: store_structured_mail

@register
@tool
def store_structured_mail(case_id: str, expected_revision: int, result: dict) -> dict:
    """① 결과를 검증·준비한다. save_agent_result와 같으며 실제 저장은 서버의 한 트랜잭션이다."""
    from app.features.work_cases.application.extraction_tools import stage
    return stage(case_id, expected_revision, result)

# ② CANDIDATE: search_supplier_evidence, crawl_supplier_candidates, create_mail_draft

# ③ INFO_PRIORITY: check_comparability, calculate_break_even

# ④ RESPONSE_PLAN: build_plan_candidates, validate_purchase_plan

# ⑤ COUNTER_OFFER: calculate_counter_offer, validate_counter_offer
