"""에이전트 도구 (07 4절). 도구 구현은 담당 팀원이 @register로 등록하고, 역할별 허용 목록은 ROLE_TOOLS가 정한다.

에이전트는 허용 목록에 있는 등록된 도구만 받는다. Gmail 자격증명·임의 SQL·무제한 웹 탐색은 도구로 주지 않는다.
분류(classify_mail_input)·전처리(preprocess_mail_documents)·발송(dispatch_mail)은 노드·백엔드 몫이라 여기 없다.

도구 작성 규칙
- 이름은 ROLE_TOOLS에 있는 이름 그대로 쓴다. 목록에 없는 이름은 register가 거부한다 (추가는 오케스트레이션 담당과 협의).
- docstring이 LLM에게 보이는 도구 설명이다. 언제 쓰는지·입력·출력을 짧게 적는다.
- 인자는 LLM이 채운다. case_id·input_revision은 인자로 받지 않고 runtime.context(RunContext)에서 읽는다.
  그 밖의 인자(근거 ID 등)도 믿지 말고 소유·업무 범위·revision을 도구 안에서 검사한다.
- 결과는 근거 ID·값·미확인 목록처럼 작게 돌려준다. 원문 전체를 돌려주지 않는다.
- 금액·수량 계산은 LLM에게 맡기지 않고 도구가 Decimal로 계산한다.
  계산은 app.features.review.domain.calculations, 숫자·단위 검증은 app.features.review.domain.validation을 재사용한다.
- 예외를 올리면 그 에이전트 호출이 실패하고 그래프가 FAILED로 끝난다. 예상 가능한 결과(없음·미확인)는 값으로 돌려준다.

예시 — 이 파일 맨 아래 역할별 구역에 추가한다.

    @register
    @tool
    def read_mail_evidence(evidence_id: str, runtime: ToolRuntime[RunContext]) -> dict:
        '''업무 범위 안의 메일·첨부 원문 구간을 읽는다. 반환: {evidence_id, text, location}'''
        case_id = runtime.context.case_id   # runtime 인자는 LLM에게 보이지 않는다
        ...

도구별 입력 → 출력 (07 4절)
| 도구                      | 역할  | 입력 → 출력                                                     | 경계                              |
| get_case_context          | 공통  | case_id → 요청 조건·회사 정책·revision                          | 소유·업무 범위 제한               |
| read_mail_evidence        | 공통  | 근거 ID → 원문 구간·위치(페이지/셀)                             | 소유·업무 범위 제한               |
| save_agent_result         | 공통  | 역할 결과·근거·revision → 결과 ID·최신 여부                     | 입력 revision 확인                |
| store_structured_mail     | ①     | 필드·품목 비고·근거 → 견적 버전 ID                              | 서버 검증 후 저장, 임의 SQL 불가  |
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
import re
from typing import Protocol

from langchain.tools import ToolRuntime
from langchain_core.tools import BaseTool, tool
from pydantic import Field

from app.agents.schemas import AgentRole, RunContext
from app.core.errors import ReviewRequired
from app.features.review.domain.schemas import FIELD_NAMES
from app.features.review.domain.validation import decimal_value, explicit_date, unit_name
from app.shared.domain.schemas import StrictModel

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

# ① INFO_EXTRACTOR: store_structured_mail
#   필드 이름은 06 quote_versions 필드가 확정될 때까지 구 추출 20필드(review.domain.schemas.FIELD_NAMES)를 쓴다.

class QuoteField(StrictModel):
    field_name: str
    raw_value: str | None              # 원문에 적힌 그대로. 원문에 없으면 null
    evidence_id: str | None = None     # raw_value가 있으면 필수
    location: str | None = None        # 페이지·셀 등 원문 위치


class ItemNote(StrictModel):
    text: str = Field(min_length=1)    # 품목 비고 원문 그대로
    evidence_id: str
    location: str | None = None


class QuoteStore(Protocol):
    """store_structured_mail의 저장소. quote_versions 테이블·RPC가 정해지면 DB 구현을 붙인다."""

    def current_revision(self, case_id: str) -> int | None:
        """업무의 현재 input_revision. 업무가 없거나 범위 밖이면 None."""

    def evidence_text(self, case_id: str, evidence_id: str) -> str | None:
        """이 업무에 속한 근거의 원문. 업무 밖 근거면 None."""

    def save_quote_version(self, case_id: str, input_revision: int, fields: list[dict], item_notes: list[dict],
                           unknown_fields: list[str]) -> str:
        """불변 견적 버전을 저장하고 견적 버전 ID를 돌려준다."""


_quote_store: QuoteStore | None = None


def use_quote_store(store: QuoteStore | None) -> None:
    global _quote_store
    _quote_store = store


_NUMBER_FIELDS = frozenset({'net_kg_per_order_unit', 'old_unit_price', 'new_unit_price', 'requested_quantity',
                            'transport_amount', 'other_logistics_amount'})
_DATE_FIELDS = frozenset({'effective_date', 'requested_delivery_date'})
_UNIT_FIELDS = frozenset({'order_unit', 'unit'})


def _compact(text: str) -> str:
    return re.sub(r'\s+', '', text)


def _parse(field_name: str, raw: str) -> str:
    """원문 값을 저장 값으로 바꾼다. 숫자는 소수 문자열, 날짜는 ISO, 단위는 표준 이름."""
    if field_name in _NUMBER_FIELDS:
        return str(decimal_value(raw, field_name))
    if field_name in _DATE_FIELDS:
        return explicit_date(raw, field_name).isoformat()
    if field_name in _UNIT_FIELDS:
        return unit_name(raw.strip())
    return raw.strip()


def _source_issue(store: QuoteStore, case_id: str, evidence_id: str | None, raw: str, texts: dict) -> str | None:
    """원문 대조. 근거가 이 업무 것이고 값이 원문에 그대로 있으면 None, 아니면 사유 코드."""
    if not evidence_id:
        return 'MISSING_EVIDENCE'
    if evidence_id not in texts:
        texts[evidence_id] = store.evidence_text(case_id, evidence_id)
    if texts[evidence_id] is None:
        return 'EVIDENCE_NOT_IN_CASE'
    if _compact(raw) not in _compact(texts[evidence_id]):
        return 'SOURCE_MISMATCH'
    return None


def save_structured_quote(store: QuoteStore, context: RunContext, fields: list[QuoteField],
                          item_notes: list[ItemNote]) -> dict:
    """store_structured_mail의 본체. 업무·revision은 실행 정보(context)에서만 받는다."""
    case_id = context.case_id
    current = store.current_revision(case_id)
    if current is None:
        return {'status': 'CASE_NOT_FOUND'}
    if current != context.input_revision:
        return {'status': 'REVISION_CHANGED'}

    texts, seen, stored, rejected, unknown = {}, set(), [], [], []
    for field in fields:
        if field.field_name not in FIELD_NAMES:
            rejected.append({'field': field.field_name, 'code': 'UNKNOWN_FIELD'})
            continue
        if field.field_name in seen:
            rejected.append({'field': field.field_name, 'code': 'DUPLICATE_FIELD'})
            continue
        seen.add(field.field_name)
        if field.raw_value is None or not field.raw_value.strip():
            unknown.append(field.field_name)
            continue
        issue = _source_issue(store, case_id, field.evidence_id, field.raw_value, texts)
        if issue is None:
            try:
                value = _parse(field.field_name, field.raw_value)
            except ReviewRequired as error:
                issue = error.issue['code']
        if issue:
            rejected.append({'field': field.field_name, 'code': issue})
            continue
        stored.append({'field_name': field.field_name, 'raw_value': field.raw_value, 'value': value,
                       'evidence_id': field.evidence_id, 'location': field.location})
    unknown += [name for name in FIELD_NAMES if name not in seen]
    if 'supplier_name' in unknown:
        rejected.append({'field': 'supplier_name', 'code': 'MISSING_FIELD'})

    notes = []
    for index, note in enumerate(item_notes):
        issue = _source_issue(store, case_id, note.evidence_id, note.text, texts)
        if issue:
            rejected.append({'field': f'item_notes[{index}]', 'code': issue})
        else:
            notes.append(note.model_dump())

    if rejected:
        return {'status': 'REJECTED', 'rejected': rejected}
    quote_version_id = store.save_quote_version(case_id, current, stored, notes, unknown)
    return {'status': 'STORED', 'quote_version_id': quote_version_id, 'unknown_fields': unknown}


@register
@tool
def store_structured_mail(fields: list[QuoteField], runtime: ToolRuntime[RunContext],
                          item_notes: list[ItemNote] | None = None) -> dict:
    '''메일에서 읽은 견적 필드를 원문과 대조·검증한 뒤 이 업무의 견적 버전으로 저장한다.

    입력: fields는 필드마다 원문 그대로의 raw_value와 근거 evidence_id·location이다. 원문에 없는 값은 raw_value를 null로 둔다.
    field_name: supplier_name, item_name, order_unit, net_kg_per_order_unit, origin, grade, processing,
    old_unit_price, new_unit_price, unit, currency, effective_date, reason, requested_quantity,
    requested_delivery_date, transport_amount, other_logistics_amount, cost_basis, cost_payer, cost_included_in_price.
    supplier_name은 필수다. item_notes는 품목 비고 원문과 근거다.

    반환 status:
    - STORED: quote_version_id와 unknown_fields(값이 없는 필드)
    - REJECTED: rejected(field·code). 아무것도 저장하지 않았다. 원문을 다시 확인해 고친 뒤 다시 호출한다.
    - REVISION_CHANGED / CASE_NOT_FOUND: 저장하지 않았다.
    '''
    if _quote_store is None or runtime.context is None:
        raise RuntimeError('quote store or run context is not configured')
    return save_structured_quote(_quote_store, runtime.context, fields, item_notes or [])


# ② CANDIDATE: search_supplier_evidence, crawl_supplier_candidates, create_mail_draft

# ③ INFO_PRIORITY: check_comparability, calculate_break_even

# ④ RESPONSE_PLAN: build_plan_candidates, validate_purchase_plan

# ⑤ COUNTER_OFFER: calculate_counter_offer, validate_counter_offer
