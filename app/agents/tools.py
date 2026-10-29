"""에이전트 도구 (07 4절). 도구 구현은 담당 팀원이 @register로 등록하고, 역할별 허용 목록은 ROLE_TOOLS가 정한다.

에이전트는 허용 목록에 있는 등록된 도구만 받는다. Gmail 자격증명·임의 SQL·무제한 웹 탐색은 도구로 주지 않는다.
분류(classify_mail_input)·전처리(preprocess_mail_documents)·발송(dispatch_mail)은 노드·백엔드 몫이라 여기 없다.

도구 작성 규칙
- 이름은 ROLE_TOOLS에 있는 이름 그대로 쓴다. 목록에 없는 이름은 register가 거부한다 (추가는 오케스트레이션 담당과 협의).
- docstring이 LLM에게 보이는 도구 설명이다. 언제 쓰는지·입력·출력을 짧게 적는다.
- 인자는 LLM이 채운다. case_id·input_revision은 인자로 받지 않고 runtime.context(RunContext)에서 읽는다.
  그 밖의 인자(메일 ID 등)도 믿지 말고 소유·업무 범위·revision을 도구 안에서 검사한다.
- DB는 직접 호출하지 않고 06 데이터 스키마 기준의 저장소 인터페이스(Protocol)를 거친다.
- 결과는 근거 ID·값·미확인 목록처럼 작게 돌려준다. 원문 전체를 돌려주지 않는다.
- 금액·수량 계산은 LLM에게 맡기지 않고 도구가 Decimal로 계산한다.
  계산은 app.features.review.domain.calculations, 숫자·단위 검증은 app.features.review.domain.validation을 재사용한다.
- 예외를 올리면 그 에이전트 호출이 실패하고 그래프가 FAILED로 끝난다. 예상 가능한 결과(없음·미확인)는 값으로 돌려준다.

예시 — 이 파일 맨 아래 역할별 구역에 추가한다.

    @register
    @tool
    def read_mail_evidence(mail_id: str, runtime: ToolRuntime[RunContext], part: str = 'body') -> dict:
        '''업무 범위 안의 메일·첨부 원문 구간을 읽는다. 반환: {mail_id, part, text, location}'''
        case_id = runtime.context.case_id   # runtime 인자는 LLM에게 보이지 않는다
        ...

도구별 입력 → 출력 (07 4절)
| 도구                      | 역할  | 입력 → 출력                                                     | 경계                              |
| get_case_context          | 공통  | case_id → 요청 조건·회사 정책·revision                          | 소유·업무 범위 제한               |
| read_mail_evidence        | 공통  | 메일 ID·본문/첨부·쪽 → 원문 구간·위치(페이지/셀)                | 소유·업무 범위 제한               |
| save_agent_result         | 공통  | 역할 결과·근거·revision → 결과 ID·최신 여부                     | 입력 revision 확인                |
| store_structured_mail     | ①     | 필드·단가 구간·비용·품목 비고·근거 → 견적 버전 ID               | 서버 검증 후 저장, 임의 SQL 불가  |
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
from decimal import Decimal
from typing import Literal, Protocol, get_args

from langchain.tools import ToolRuntime
from langchain_core.tools import BaseTool, tool
from pydantic import Field, StrictBool

from app.agents.schemas import AGENT_IO, READ_RETRY_LIMIT, AgentRole, DecimalStr, RunContext
from app.core.errors import ReviewRequired
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
#   06 work_cases·mail_messages·agent_runs 기준. 업무·revision·실행 ID는 runtime.context(RunContext),
#   역할은 build_agent가 정한 에이전트 이름에서 읽는다 (LLM 인자 아님).

MAX_EVIDENCE_CHARS = 4000            # read_mail_evidence가 한 번에 돌려주는 원문 길이 (제안값)
_CASE_KEYS = ('input_revision', 'item_name', 'specification', 'mandatory_conditions', 'quantity', 'unit', 'due_date',
              'policy', 'latest_quote_version_ids')
_MAIL_KEYS = ('mail_id', 'message_role', 'direction', 'supplier_id', 'received_at', 'subject', 'attachments')


class CaseStore(Protocol):
    """공통 도구의 저장소 (06 work_cases·mail_messages·agent_runs). 소유 범위는 구현이 검사한다."""

    def current_input_revision(self, case_id: str) -> int | None:
        """work_cases.input_revision. 업무가 없거나 범위 밖이면 None."""

    def get_work_case(self, case_id: str) -> dict | None:
        """work_cases의 {input_revision, item_name, specification, mandatory_conditions(mandatory_conditions_jsonb),
        quantity, unit, due_date, policy(policy_jsonb), latest_quote_version_ids(업체별 최신 quote_versions.id)}.
        업무가 없거나 범위 밖이면 None."""

    def list_case_mails(self, case_id: str) -> list[dict]:
        """업무에 연결된 mail_messages의 {mail_id, message_role, direction, supplier_id, received_at, subject,
        attachments: [{attachment_ref, filename, pages}]}. 본문은 넣지 않는다."""

    def read_mail_part(self, case_id: str, mail_id: str, part: str, page: int | None) -> dict | None:
        """업무에 속한 메일의 본문(part='body') 또는 첨부(part=attachment_ref) 한 쪽의 {text, location}.
        body_text와 normalized_jsonb의 문서 읽기·OCR 결과에서 읽는다. 업무 밖이면 None."""

    def save_agent_output(self, case_id: str, agent_run_id: str, role: str, expected_input_revision: int,
                          output: dict) -> bool:
        """agent_runs.output_jsonb에 결과를 합친다. 같은 트랜잭션에서 input_revision이 다르면 저장하지 않고 False."""


_case_store: CaseStore | None = None


def use_case_store(store: CaseStore | None) -> None:
    global _case_store
    _case_store = store


def _run_context(store, runtime: ToolRuntime) -> RunContext:
    if store is None or runtime.context is None:
        raise RuntimeError('store or run context is not configured')
    return runtime.context


def _agent_role(runtime: ToolRuntime) -> AgentRole:
    """build_agent가 create_agent(name=role.lower())로 정한 이름. LLM이 바꿀 수 없다."""
    return AgentRole(str((runtime.config or {}).get('metadata', {}).get('lc_agent_name', '')).upper())


def _read(load):
    """조회 예외는 READ_RETRY_LIMIT번까지 다시 시도하고, 그래도 실패하면 올린다."""
    for attempt in range(READ_RETRY_LIMIT + 1):
        try:
            return load()
        except Exception:
            if attempt == READ_RETRY_LIMIT:
                raise


def load_case_context(store: CaseStore, context: RunContext) -> dict:
    case = _read(lambda: store.get_work_case(context.case_id))
    if case is None:
        return {'status': 'CASE_NOT_FOUND'}
    if case.get('input_revision') != context.input_revision:
        return {'status': 'REVISION_CHANGED'}
    mails = _read(lambda: store.list_case_mails(context.case_id))
    return {'status': 'OK', **{key: case.get(key) for key in _CASE_KEYS},
            'mails': [{key: mail.get(key) for key in _MAIL_KEYS} for mail in mails]}


def load_evidence(store: CaseStore, context: RunContext, mail_id: str, part: str, page: int | None) -> dict:
    item = _read(lambda: store.read_mail_part(context.case_id, mail_id, part, page))
    if item is None:
        return {'status': 'NOT_FOUND', 'mail_id': mail_id, 'part': part, 'page': page}
    text = item['text']
    return {'status': 'OK', 'mail_id': mail_id, 'part': part, 'page': page, 'text': text[:MAX_EVIDENCE_CHARS],
            'location': item.get('location'), 'truncated': len(text) > MAX_EVIDENCE_CHARS}


def save_result(store: CaseStore, context: RunContext, role: AgentRole, next_action: str, reason: str,
                unknown_fields: list[str], source_quote_ids: list[str], source_run_ids: list[str]) -> dict:
    if context.agent_run_id is None:
        raise RuntimeError('agent run is not recorded')
    if next_action not in get_args(AGENT_IO[role][1].model_fields['next_action'].annotation):
        return {'status': 'REJECTED', 'code': 'INVALID_NEXT_ACTION'}
    if not 1 <= len(reason.strip()) <= 500:
        return {'status': 'REJECTED', 'code': 'INVALID_REASON'}
    current = store.current_input_revision(context.case_id)
    if current is None:
        return {'status': 'CASE_NOT_FOUND'}
    output = {'next_action': next_action, 'reason': reason.strip(), 'unknown_fields': unknown_fields,
              'source_quote_ids': source_quote_ids, 'source_run_ids': source_run_ids}
    if current != context.input_revision or not store.save_agent_output(
            context.case_id, context.agent_run_id, role, context.input_revision, output):
        return {'status': 'REVISION_CHANGED', 'is_latest': False}
    return {'status': 'SAVED', 'result_id': context.agent_run_id, 'is_latest': True}


@register
@tool
def get_case_context(runtime: ToolRuntime[RunContext]) -> dict:
    '''이 업무의 품목·규격·필수 조건·수량·납기·회사 정책·현재 input_revision·업체별 최신 견적 버전 ID와
    업무에 연결된 메일 목록(mail_id·message_role·업체·첨부 attachment_ref·쪽수)을 조회한다. 본문은 read_mail_evidence로 읽는다.

    반환 status: OK / REVISION_CHANGED(input_revision이 바뀌었다. 멈춘다) / CASE_NOT_FOUND
    '''
    return load_case_context(_case_store, _run_context(_case_store, runtime))


@register
@tool
def read_mail_evidence(mail_id: str, runtime: ToolRuntime[RunContext], part: str = 'body',
                       page: int | None = None) -> dict:
    '''이 업무에 속한 메일의 본문(part='body') 또는 첨부(part=attachment_ref) 한 쪽(page)의 원문과 위치를 읽는다.

    반환 status: OK면 text·location·truncated(원문이 길어 앞부분만 줬으면 true) / NOT_FOUND(이 업무의 메일·첨부가 아님)
    '''
    return load_evidence(_case_store, _run_context(_case_store, runtime), mail_id, part, page)


@register
@tool
def save_agent_result(next_action: str, reason: str, runtime: ToolRuntime[RunContext],
                      unknown_fields: list[str] | None = None, source_quote_ids: list[str] | None = None,
                      source_run_ids: list[str] | None = None) -> dict:
    '''이번 역할 실행의 결과(next_action·reason·unknown_fields·참조한 견적 버전 ID·이전 역할 실행 ID)를 저장한다.
    next_action은 이 역할이 고를 수 있는 값이어야 하고 reason은 1~500자다.

    반환 status: SAVED면 result_id·is_latest / REVISION_CHANGED(저장 안 함) / REJECTED(code) / CASE_NOT_FOUND
    '''
    context = _run_context(_case_store, runtime)
    return save_result(_case_store, context, _agent_role(runtime), next_action, reason, unknown_fields or [],
                       source_quote_ids or [], source_run_ids or [])


# ① INFO_EXTRACTOR: store_structured_mail
#   06 quote_versions 기준. 견적은 불변 버전으로 쌓고(version_no·supersedes_id), 이번 메일에 값이 없는 필드는 같은 업체의
#   이전 버전에서 원래 근거와 함께 이어받는다. 견적 저장은 input_revision을 올리지 않는다(백엔드가 새 회신 등록 시 올린다).

SCHEMA_VERSION = 'quote_versions.06.v1'

# 필드 이름 → (값 종류, 06 저장 위치)
#   supplier: suppliers로 연결 / column: quote_versions 열 / pricing: pricing_terms_jsonb / checks: checks_jsonb
#   mail: 견적이 아닌 메일 정보라 mail_messages.normalized_jsonb에만 둔다
QUOTE_FIELDS = {
    'supplier_name': ('text', 'supplier'),
    'item_name': ('text', 'checks'),
    'specification': ('text', 'checks'),
    'origin': ('text', 'checks'),
    'certifications': ('text', 'checks'),
    'tax_terms': ('text', 'checks'),
    'lead_time': ('text', 'checks'),
    'delivery_date': ('date', 'checks'),
    'unit_price': ('number', 'column'),
    'unit': ('unit', 'column'),
    'available_quantity': ('number', 'column'),
    'effective_from': ('date', 'column'),
    'valid_until': ('date', 'column'),
    'currency': ('currency', 'pricing'),
    'order_unit': ('unit', 'pricing'),
    'quantity_per_order_unit': ('number', 'pricing'),
    'moq': ('number', 'pricing'),
    'pack_multiple': ('number', 'pricing'),
    'previous_unit_price': ('number', 'mail'),
    'increase_reason': ('text', 'mail'),
}
_KRW = {'KRW', '원', '₩', '원화', '원(KRW)'}
_FREE = {'무료', '무상'}


class EvidenceRef(StrictModel):
    mail_id: str                       # mail_messages.id
    part: str = 'body'                 # 'body' 또는 첨부 attachment_ref
    page: int | None = Field(default=None, ge=1)
    cell: str | None = None            # 표의 행·열 등


class QuoteField(StrictModel):
    field_name: str
    raw_value: str | None              # 원문에 적힌 그대로. 원문에 없으면 null
    evidence: EvidenceRef | None = None


class PriceTier(StrictModel):
    min_quantity: str | None = None    # 원문 그대로
    max_quantity: str | None = None
    unit_price: str
    evidence: EvidenceRef


class CostTerm(StrictModel):
    cost_type: Literal['SHIPPING', 'HANDLING', 'OTHER']
    source_text: str = Field(min_length=1)  # 이 비용 조건이 적힌 원문 구절 그대로
    amount: str | None = None          # 원문 그대로. 무료 근거가 있으면 '무료'
    basis: str | None = None           # 산정 기준 원문 (예: 회당, 박스당)
    payer: str | None = None           # 부담 주체 원문
    included_in_price: StrictBool | None = None  # 원문에 단가 포함·별도가 적혀 있을 때만
    evidence: EvidenceRef


class ItemNote(StrictModel):
    text: str = Field(min_length=1)    # 품목 비고 원문 그대로
    evidence: EvidenceRef


class QuoteVersionRecord(StrictModel):
    """quote_versions 한 행 (06). 저장소 구현이 mail_normalized·case_conditions와 함께 한 트랜잭션으로 저장한다."""
    work_case_id: str
    supplier_id: str
    source_mail_id: str
    version_no: int = Field(ge=1)
    supersedes_id: str | None
    unit_price: DecimalStr | None
    unit: str | None
    shipping_cost: DecimalStr | None
    available_quantity: DecimalStr | None
    effective_from: str | None
    valid_until: str | None
    checks_jsonb: dict
    pricing_terms_jsonb: dict
    cost_terms_jsonb: list[dict]
    item_notes_jsonb: list[dict]
    evidence_jsonb: dict               # 필드 → {raw_value, value, mail_id, location, carried_from?}
    extraction_status: Literal['NEEDS_REVIEW', 'CONFIRMED', 'UNSUPPORTED']
    mail_normalized: dict              # mail_id → mail_messages.normalized_jsonb
    case_conditions: dict | None       # 최초 추출 시 work_cases.item_name·specification


class QuoteStore(CaseStore, Protocol):
    """store_structured_mail의 저장소 (06 suppliers·quote_versions·mail_messages·work_cases)."""

    def resolve_supplier(self, case_id: str, mail_id: str, supplier_name: str) -> str | None:
        """메일에 연결된 mail_messages.supplier_id를 먼저, 없으면 suppliers.name이 정확히 같은 업체. 없으면 None.
        거래처를 새로 만들지 않는다."""

    def latest_quote_version(self, case_id: str, supplier_id: str) -> tuple[str, QuoteVersionRecord] | None:
        """이 업무·업체의 최신 quote_versions (id, 행)."""

    def insert_quote_version(self, record: QuoteVersionRecord, expected_input_revision: int) -> str | None:
        """quote_versions·price_history·mail_messages.normalized_jsonb·work_cases 조건·workflow_logs를 한 트랜잭션으로
        저장하고 quote_versions.id를 돌려준다. input_revision은 올리지 않는다. revision이 다르면 저장하지 않고 None.
        같은 원문 메일·같은 견적 내용(version_no·supersedes_id·case_conditions 제외)이면 기존 id를 돌려준다."""


_quote_store: QuoteStore | None = None


def use_quote_store(store: QuoteStore | None) -> None:
    global _quote_store
    _quote_store = store


def _compact(text: str) -> str:
    return re.sub(r'\s+', '', text)


def _parse(kind: str, field_name: str, raw: str) -> str:
    """원문 값을 저장 값으로 바꾼다. 숫자는 소수 문자열, 날짜는 ISO, 단위·통화는 표준 이름."""
    if kind == 'number':
        return str(decimal_value(raw, field_name))
    if kind == 'date':
        return explicit_date(raw, field_name).isoformat()
    if kind == 'unit':
        return unit_name(raw.strip())
    if kind == 'currency':
        return 'KRW' if raw.strip() in _KRW else raw.strip().upper()
    return raw.strip()


class _SourceCheck:
    """근거가 이 업무의 메일·첨부 쪽이고 값이 그 쪽 원문에 그대로 있는지 확인한다. 쪽 원문은 한 번만 읽는다."""

    def __init__(self, store: CaseStore, case_id: str):
        self.store, self.case_id, self.parts = store, case_id, {}

    def __call__(self, evidence: EvidenceRef | None, raw: str) -> tuple[str | None, dict | None]:
        if evidence is None:
            return 'MISSING_EVIDENCE', None
        key = (evidence.mail_id, evidence.part, evidence.page)
        if key not in self.parts:
            self.parts[key] = _read(lambda: self.store.read_mail_part(self.case_id, *key))
        part = self.parts[key]
        if part is None:
            return 'EVIDENCE_NOT_IN_CASE', None
        if _compact(raw) not in _compact(part['text']):
            return 'SOURCE_MISMATCH', None
        location = ' '.join(filter(None, [part.get('location'), evidence.cell]))
        return None, {'mail_id': evidence.mail_id, 'location': location}


def _number(check, raw: str | None, name: str, evidence: EvidenceRef, free_is_zero=False):
    """(값, 사유 코드). 원문에 없으면 (None, None)."""
    if raw is None or not raw.strip():
        return None, None
    issue, _ = check(evidence, raw)
    if issue:
        return None, issue
    if free_is_zero and raw.strip() in _FREE:
        return '0', None
    try:
        return str(decimal_value(raw, name)), None
    except ReviewRequired as error:
        return None, error.issue['code']


def _tiers(check, tiers: list[PriceTier], rejected: list) -> list[dict]:
    out = []
    for index, tier in enumerate(tiers):
        tier_values, issue = {}, None
        for name in ('unit_price', 'min_quantity', 'max_quantity'):
            tier_values[name], issue = _number(check, getattr(tier, name), name, tier.evidence)
            if issue:
                break
        if issue:
            rejected.append({'field': f'price_tiers[{index}]', 'code': issue})
            continue
        out.append(tier_values | check(tier.evidence, tier.unit_price)[1])
    bounded = sorted((t for t in out if t['min_quantity'] is not None), key=lambda t: Decimal(t['min_quantity']))
    for before, after in zip(bounded, bounded[1:]):
        if before['max_quantity'] is not None and Decimal(after['min_quantity']) < Decimal(before['max_quantity']):
            rejected.append({'field': 'price_tiers', 'code': 'TIER_OVERLAP'})
            break
    return out


def _costs(check, costs: list[CostTerm], rejected: list) -> list[dict]:
    out = []
    for index, cost in enumerate(costs):
        issue, source = check(cost.evidence, cost.source_text)
        for text in (cost.amount, cost.basis, cost.payer):
            if not issue and text and _compact(text) not in _compact(cost.source_text):
                issue = 'SOURCE_MISMATCH'
        amount = None
        if not issue:
            amount, issue = _number(check, cost.amount, 'amount', cost.evidence, free_is_zero=True)
        if issue:
            rejected.append({'field': f'cost_terms[{index}]', 'code': issue})
            continue
        out.append({'cost_type': cost.cost_type, 'source_text': cost.source_text, 'amount': amount,
                    'basis': cost.basis, 'payer': cost.payer, 'included_in_price': cost.included_in_price, **source})
    return out


def save_structured_quote(store: QuoteStore, context: RunContext, fields: list[QuoteField],
                          price_tiers: list[PriceTier], cost_terms: list[CostTerm], item_notes: list[ItemNote]) -> dict:
    """store_structured_mail의 본체. 업무·revision은 실행 정보(context)에서만 받는다."""
    case_id = context.case_id
    case = _read(lambda: store.get_work_case(case_id))
    if case is None:
        return {'status': 'CASE_NOT_FOUND'}
    if case.get('input_revision') != context.input_revision:
        return {'status': 'REVISION_CHANGED'}

    check, rejected, values, sent_null, seen = _SourceCheck(store, case_id), [], {}, [], set()
    for field in fields:
        if field.field_name not in QUOTE_FIELDS:
            rejected.append({'field': field.field_name, 'code': 'UNKNOWN_FIELD'})
            continue
        if field.field_name in seen:
            rejected.append({'field': field.field_name, 'code': 'DUPLICATE_FIELD'})
            continue
        seen.add(field.field_name)
        if field.raw_value is None or not field.raw_value.strip():
            sent_null.append(field.field_name)
            continue
        issue, source = check(field.evidence, field.raw_value)
        if issue is None:
            try:
                value = _parse(QUOTE_FIELDS[field.field_name][0], field.field_name, field.raw_value)
            except ReviewRequired as error:
                issue = error.issue['code']
        if issue:
            rejected.append({'field': field.field_name, 'code': issue})
            continue
        values[field.field_name] = {'raw_value': field.raw_value, 'value': value, **source}
    tiers = _tiers(check, price_tiers, rejected)
    costs = _costs(check, cost_terms, rejected)
    notes = []
    for index, note in enumerate(item_notes):
        issue, source = check(note.evidence, note.text)
        if issue:
            rejected.append({'field': f'item_notes[{index}]', 'code': issue})
        else:
            notes.append({'text': note.text, **source})

    if 'supplier_name' not in values:
        rejected.append({'field': 'supplier_name', 'code': 'MISSING_FIELD'})
    case_item = case.get('item_name')
    if case_item and 'item_name' in values and _compact(values['item_name']['value']) != _compact(case_item):
        rejected.append({'field': 'item_name', 'code': 'ITEM_OUT_OF_CASE'})
    if rejected:
        return {'status': 'REJECTED', 'rejected': rejected}

    source_mail_id = values['supplier_name']['mail_id']
    supplier_id = _read(lambda: store.resolve_supplier(case_id, source_mail_id, values['supplier_name']['value']))
    if supplier_id is None:
        return {'status': 'REJECTED', 'rejected': [{'field': 'supplier_name', 'code': 'SUPPLIER_NOT_MATCHED'}]}

    current = dict(values)
    previous = _read(lambda: store.latest_quote_version(case_id, supplier_id))
    carried = []
    if previous:
        previous_id, previous_record = previous
        for name, entry in previous_record.evidence_jsonb.items():
            if name not in values and QUOTE_FIELDS.get(name, ('', 'mail'))[1] not in ('mail', 'supplier'):
                values[name] = {**entry, 'carried_from': entry.get('carried_from', previous_id)}
                carried.append(name)
        if not tiers and previous_record.pricing_terms_jsonb.get('price_tiers'):
            tiers = previous_record.pricing_terms_jsonb['price_tiers']
            carried.append('price_tiers')
        if not costs and previous_record.cost_terms_jsonb:
            costs = previous_record.cost_terms_jsonb
            carried.append('cost_terms')
    if 'unit_price' in values and 'unit' not in values:
        return {'status': 'REJECTED', 'rejected': [{'field': 'unit', 'code': 'MISSING_FIELD'}]}

    def value(name):
        return values[name]['value'] if name in values else None

    def grouped(target):
        return {name: values[name]['value'] for name, (_, where) in QUOTE_FIELDS.items()
                if where == target and name in values}

    mails = {}
    for name, entry in current.items():
        mails.setdefault(entry['mail_id'], {'schema_version': SCHEMA_VERSION, 'fields': {}, 'item_notes': []})
        mails[entry['mail_id']]['fields'][name] = entry
    for note in notes:
        mails.setdefault(note['mail_id'], {'schema_version': SCHEMA_VERSION, 'fields': {}, 'item_notes': []})
        mails[note['mail_id']]['item_notes'].append(note)
    shipping = next((cost['amount'] for cost in costs if cost['cost_type'] == 'SHIPPING'
                     and cost['amount'] is not None and cost['included_in_price'] is False), None)
    record = QuoteVersionRecord(
        work_case_id=case_id, supplier_id=supplier_id, source_mail_id=source_mail_id,
        version_no=previous[1].version_no + 1 if previous else 1, supersedes_id=previous[0] if previous else None,
        unit_price=value('unit_price'), unit=value('unit'), shipping_cost=shipping,
        available_quantity=value('available_quantity'), effective_from=value('effective_from'),
        valid_until=value('valid_until'),
        checks_jsonb={name: {'state': 'STATED', 'value': v} for name, v in grouped('checks').items()},
        pricing_terms_jsonb=grouped('pricing') | ({'price_tiers': tiers} if tiers else {}),
        cost_terms_jsonb=costs, item_notes_jsonb=notes,
        evidence_jsonb={name: entry for name, entry in values.items() if QUOTE_FIELDS[name][1] != 'mail'},
        extraction_status='NEEDS_REVIEW' if value('currency') not in (None, 'KRW') else 'CONFIRMED',
        mail_normalized=mails,
        case_conditions={'item_name': value('item_name'), 'specification': value('specification')}
        if not case_item and 'item_name' in current else None)
    quote_version_id = store.insert_quote_version(record, context.input_revision)
    if quote_version_id is None:
        return {'status': 'REVISION_CHANGED'}
    return {'status': 'STORED', 'quote_version_id': quote_version_id, 'version_no': record.version_no,
            'supersedes_id': record.supersedes_id, 'extraction_status': record.extraction_status,
            'unknown_fields': [name for name in sent_null if name not in values], 'carried_fields': carried}


@register
@tool
def store_structured_mail(fields: list[QuoteField], runtime: ToolRuntime[RunContext],
                          price_tiers: list[PriceTier] | None = None, cost_terms: list[CostTerm] | None = None,
                          item_notes: list[ItemNote] | None = None) -> dict:
    '''메일에서 읽은 견적을 원문과 대조·검증한 뒤 이 업무의 새 견적 버전으로 저장한다.

    입력: 모든 값은 원문에 적힌 그대로, 근거 evidence{mail_id, part('body' 또는 attachment_ref), page, cell}와 함께 준다.
    fields: field_name과 raw_value. 원문에서 찾았지만 값이 없으면 raw_value를 null로 둔다.
      supplier_name(필수), item_name, specification, origin, certifications, tax_terms, lead_time, delivery_date,
      unit_price, unit(unit_price가 있으면 필수), available_quantity, effective_from, valid_until, currency,
      order_unit, quantity_per_order_unit, moq, pack_multiple, previous_unit_price(인상 전 단가), increase_reason(인상 사유)
    price_tiers: 단가 구간 min_quantity·max_quantity·unit_price
    cost_terms: 비용 cost_type(SHIPPING/HANDLING/OTHER)·source_text(그 비용이 적힌 원문 구절)·amount·basis·payer·
      included_in_price(원문에 단가 포함·별도가 적혀 있을 때만 true/false)
    item_notes: 품목 비고 원문
    같은 업체의 이전 견적에 있던 값은 도구가 이어받으므로 이번 메일에 없는 값을 다시 적지 않는다.

    반환 status:
    - STORED: quote_version_id·version_no·extraction_status·unknown_fields(null로 보낸 필드 중 여전히 값이 없는 것)
    - REJECTED: rejected(field·code). 아무것도 저장하지 않았다.
    - REVISION_CHANGED / CASE_NOT_FOUND: 저장하지 않았다.
    '''
    context = _run_context(_quote_store, runtime)
    return save_structured_quote(_quote_store, context, fields, price_tiers or [], cost_terms or [], item_notes or [])
