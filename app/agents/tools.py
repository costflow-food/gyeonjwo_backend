"""에이전트 도구 (07 4절). 도구는 @register로 등록하고, 역할별 허용 목록은 ROLE_TOOLS가 정한다.

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
  숫자·단위 검증은 app.features.review.domain.validation을 재사용한다.
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
| store_structured_mail     | ①     | 필드·단가 구간·비용·품목 비고·근거 → 견적 버전 ID               | 서버 검증 후 저장, 임의 SQL 불가  |
| search_supplier_items     | ②     | 검색어 → 품목명에 검색어가 든 등록 품목(업체·품목·규격)          | 가격 없음, 결과 상한              |
| search_supplier_evidence  | ②     | 같은 품목으로 고른 등록 품목 → 취급 근거·후보                   | 과거 단가를 현재 가격으로 안 줌   |
| crawl_supplier_candidates | ②     | 허용 출처·조건 → 잠정 후보·연락처·출처 시점 (Should)            | 잠정 후보, 회신으로 확인          |
| check_comparability       | ③④⑤   | 견적 버전·정책 → 누락·불일치·비교 범위                          | 필수·가중 조건 구분               |
| calculate_break_even      | ③     | 확인된 비용·제약 → 역전 기준 금액·산식                          | 미확인 금액을 0원으로 보지 않음   |
| build_plan_candidates     | ④     | 견적·정책 → 유지·전환·분산(최대 2곳) 안                          | 단가 구간·MOQ·납기·비용 중복 검사 |
| validate_purchase_plan    | ④     | 배분안 → 총액·위반 사유·탐색 범위                               | 위와 같음                         |
| calculate_counter_offer   | ⑤     | 기존·요청 가격·대안 총액·회사 정책 → 목표 조건별 비용            | 정책 없는 인상률 생성 금지        |
| validate_counter_offer    | ⑤     | 목표 조건 → 가격 범위·정책 검증 결과                            | 위와 같음                         |
| create_rfq_draft          | ②     | 고른 등록 품목·대상 업체·선택 질문 → 업체별 초안 ID              | 내부 저장만, 발송은 백엔드        |
| create_clarification_draft| ③     | 업체·질문 묶음·근거 → 보완 요청 초안 ID                          | 내부 저장만, 발송은 백엔드        |
| create_counter_offer_draft| ⑤     | 목표 단가·조건·비교한 대안 → 다시 검증 후 역제안 초안 ID          | 내부 저장만, 발송은 백엔드        |
"""
import dataclasses
import hashlib
import json
import re
import unicodedata
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Literal, Protocol

from langchain.tools import ToolRuntime
from langchain_core.messages import ToolMessage
from langchain_core.tools import BaseTool, tool
from pydantic import Field, StrictBool

from app.agents.schemas import READ_RETRY_LIMIT, AgentRole, DecimalStr, RunContext
from app.core.errors import ReviewRequired, ServiceError
from app.features.review.domain.validation import decimal_value, explicit_date, unit_name
from app.shared.domain.schemas import StrictModel

# 역할 결과는 AgentRunner가 최종 응답으로 agent_runs에 저장한다. 저장 도구는 없다
_READ = ('get_case_context', 'read_mail_evidence')

ROLE_TOOLS = {
    AgentRole.INFO_EXTRACTOR: _READ + ('store_structured_mail',),
    AgentRole.CANDIDATE: _READ + ('search_supplier_items', 'search_supplier_evidence', 'crawl_supplier_candidates',
                                  'create_rfq_draft'),
    AgentRole.INFO_PRIORITY: _READ + ('check_comparability', 'calculate_break_even', 'create_clarification_draft'),
    AgentRole.RESPONSE_PLAN: _READ + ('check_comparability', 'build_plan_candidates', 'validate_purchase_plan'),
    AgentRole.COUNTER_OFFER: _READ + ('check_comparability', 'calculate_counter_offer', 'validate_counter_offer',
                                        'create_counter_offer_draft'),
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
# 도구 구현
# ─────────────────────────────────────────────

# 공통: get_case_context, read_mail_evidence
#   06 work_cases·mail_messages·agent_runs 기준. 업무·revision·실행 ID는 runtime.context(RunContext),
#   역할은 build_agent가 정한 에이전트 이름에서 읽는다 (LLM 인자 아님).

# read_mail_evidence가 한 번에 돌려주는 원문 길이. 단가표 한 쪽을 대개 한 번에 읽어 도구 호출 한도를 아낀다
MAX_EVIDENCE_CHARS = 12000
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
        attachments: [{attachment_ref, filename, pages, status}]}. 본문은 넣지 않는다.
        status는 전처리 노드가 읽은 결과(READ·NO_TEXT·UNSUPPORTED 등, nodes.read_document)다."""

    def read_mail_part(self, case_id: str, mail_id: str, part: str, page: int | None) -> dict | None:
        """업무에 속한 메일의 본문(part='body') 또는 첨부(part=attachment_ref) 한 쪽의 {text, location}.
        body_text와 normalized_jsonb의 문서 읽기·OCR 결과에서 읽는다. 업무 밖이면 None.
        선택 키 — text_source: 'TEXT'(텍스트 레이어·본문, 없으면 이것) 또는 'OCR'(documents.ocr_pdf_page 결과)
          unreadable: true면 그 쪽을 읽지 못했다(text는 ''). attempts·retry_exhausted는 ocr_pdf_page의 시도 기록이다."""


_case_store: CaseStore | None = None


def use_case_store(store: CaseStore | None) -> None:
    global _case_store
    _case_store = store


def _run_context(store, runtime: ToolRuntime) -> RunContext:
    if store is None or runtime.context is None:
        raise RuntimeError('store or run context is not configured')
    return runtime.context


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


def load_evidence(store: CaseStore, context: RunContext, mail_id: str, part: str, page: int | None,
                  offset: int = 0) -> dict:
    try:
        item = _read(lambda: store.read_mail_part(context.case_id, mail_id, part, page))
    except ServiceError as error:
        if error.code != 'EXTRACTION_DOCUMENT_LIMIT':
            raise
        # 업무 한 번 실행에서 읽을 수 있는 원문 양(12만 자)을 넘었다. 일부만 읽고 판단하지 않는다
        return {'status': 'LIMIT_EXCEEDED', 'mail_id': mail_id, 'part': part, 'page': page,
                'next': '원문이 처리 한도를 넘었다. 다른 쪽을 더 읽지 말고 바로 HOLD로 최종 응답한다. '
                        'hold.code가 있으면 SOURCE_READ_FAILED로 하고, hold가 없는 역할은 reason에 SOURCE_READ_FAILED를 함께 적는다.'}
    if item is None:
        result = {'status': 'NOT_FOUND', 'mail_id': mail_id, 'part': part, 'page': page}
        mail = next((row for row in _read(lambda: store.list_case_mails(context.case_id))
                     if row['mail_id'] == mail_id), None)
        if mail is not None:     # 이 업무의 메일이면 읽을 수 있는 part를 알려준다. 다른 업무의 메일은 알려주지 않는다
            result['available_parts'] = [{'part': 'body'}] + [
                {'part': attachment['attachment_ref'], 'pages': attachment['pages']}
                for attachment in mail.get('attachments') or []]
        return result
    if item.get('unreadable'):
        # 기술 오류로 시도를 다 쓴 쪽 등. 다시 불러도 저장소가 같은 결과를 돌려준다
        return {'status': 'UNREADABLE', 'mail_id': mail_id, 'part': part, 'page': page,
                'attempts': item.get('attempts'), 'retry_exhausted': bool(item.get('retry_exhausted'))}
    text = item['text']
    if type(offset) is not int or not 0 <= offset <= max(len(text) - 1, 0):
        return {'status': 'NOT_FOUND', 'mail_id': mail_id, 'part': part, 'page': page, 'offset': offset,
                'length': len(text)}
    end = offset + MAX_EVIDENCE_CHARS
    # empty: 글자가 없는 쪽(스캔 이미지 등). 이 쪽에서는 어떤 값도 근거가 되지 않는다
    # text_source OCR: 그림을 글자로 읽은 결과라 잘못 읽은 글자가 있을 수 있다
    result = {'status': 'OK', 'mail_id': mail_id, 'part': part, 'page': page, 'offset': offset,
              'text': text[offset:end], 'location': item.get('location'), 'truncated': len(text) > end,
              'empty': not text.strip(), 'text_source': item.get('text_source') or 'TEXT'}
    if len(text) > end:     # 긴 단가표의 뒷부분은 같은 쪽을 next_offset부터 다시 읽는다
        result['next_offset'] = end
    return result


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
def crawl_supplier_candidates(query: str, runtime: ToolRuntime[RunContext], limit: int = 5) -> dict:
    '''업무의 원료 품목·상태에 대한 공개 웹 검색 결과와 홈페이지 연락처를 수집한다.
    query에는 검색할 품목과 조건만 넣고 메일 본문·개인정보는 넣지 않는다. limit은 1~5이다.
    검색 결과는 미등록·미검증 자료다. 업체 ID나 견적 초안 대상으로 사용하지 않는다.
    반환: OK / EMPTY / ERROR / REVISION_CHANGED / CASE_NOT_FOUND.
    '''
    from app.features.supplier_crawl.application.tool import SupplierSearchInput
    from app.features.supplier_crawl.infrastructure.google_search import search_suppliers

    context = _run_context(_case_store, runtime)
    request = SupplierSearchInput(query=query, limit=limit)
    if request.limit > 5:
        return {'status': 'ERROR', 'code': 'LIMIT_EXCEEDED', 'suppliers': []}
    # 한 실행에서 웹 검색은 WEB_SEARCH_CALLS_PER_RUN번까지다(프롬프트의 "한 번"을 코드로도 지킨다). 외부 호출이라 비용·차단 위험이 있다
    done = [m for m in ((getattr(runtime, 'state', None) or {}).get('messages') or [])
            if isinstance(m, ToolMessage) and m.name == 'crawl_supplier_candidates']
    if len(done) >= WEB_SEARCH_CALLS_PER_RUN:
        return {'status': 'ERROR', 'code': 'SEARCH_LIMIT_REACHED', 'suppliers': [],
                'hint': '웹 검색은 한 번만 한다. 다시 부르지 말고 앞선 결과로 최종 응답한다.'}
    case = _read(lambda: _case_store.get_work_case(context.case_id))
    if case is None:
        return {'status': 'CASE_NOT_FOUND', 'suppliers': []}
    if case.get('input_revision') != context.input_revision:
        return {'status': 'REVISION_CHANGED', 'suppliers': []}
    result = search_suppliers(request.query, request.limit)
    case = _read(lambda: _case_store.get_work_case(context.case_id))
    if case is None:
        return {'status': 'CASE_NOT_FOUND', 'suppliers': []}
    if case.get('input_revision') != context.input_revision:
        return {'status': 'REVISION_CHANGED', 'suppliers': []}
    return result


@register
@tool
def read_mail_evidence(mail_id: str, runtime: ToolRuntime[RunContext], part: str = 'body',
                       page: int | None = None, offset: int = 0) -> dict:
    '''이 업무에 속한 메일의 본문 또는 첨부 한 쪽(page)의 원문과 위치를 읽는다. 한 번에 12,000자까지 준다.
    part: 본문은 'body', 첨부는 get_case_context 메일 목록의 attachment_ref 값(예: 'att-1'). 'attachment_ref'라는 글자가 아니다.
    page: 첨부의 쪽 번호(1부터). 본문은 비운다.
    offset: 그 본문·쪽에서 읽기 시작할 글자 위치(0부터). 처음에는 비운다. truncated면 next_offset을 넣어 이어 읽는다.

    반환 status: OK면 text·location·truncated(뒤에 더 있으면 true. 같은 part·page를 offset=next_offset으로 다시 읽는다)·
      empty(글자가 없는 쪽)·
      text_source(TEXT 또는 OCR: 스캔 그림을 글자로 읽은 쪽. 원문 그대로 옮기되 없는 글자를 짐작해 채우지 않는다)
      / NOT_FOUND(이 업무의 메일인데 part·page가 틀렸으면 available_parts로 읽을 수 있는 part·쪽수를 알려준다.
        offset이 범위 밖이면 length로 그 본문·쪽의 글자 수를 알려준다)
      / UNREADABLE(이 쪽을 읽지 못했다. 다시 불러도 같다. 이 쪽의 값은 근거로 쓰지 않는다)
      / LIMIT_EXCEEDED(원문이 처리 한도를 넘었다. 더 읽지 말고 next대로 HOLD한다)
    '''
    return load_evidence(_case_store, _run_context(_case_store, runtime), mail_id, part, page, offset)


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
    'unit_price': ('price', 'column'),
    'unit': ('unit', 'column'),
    'available_quantity': ('number', 'column'),
    'effective_from': ('date', 'column'),
    'valid_until': ('date', 'column'),
    'currency': ('currency', 'pricing'),
    'order_unit': ('unit', 'pricing'),
    'quantity_per_order_unit': ('number', 'pricing'),
    'moq': ('number', 'pricing'),
    'pack_multiple': ('number', 'pricing'),
    'previous_unit_price': ('price', 'mail'),
    'increase_reason': ('text', 'mail'),
}
# fields가 아니라 각 인자로 받는 항목. fields에 넣으면 값이 null이어도 UNKNOWN_FIELD로 거절한다
ARGUMENT_FIELDS = ('price_tiers', 'cost_terms', 'item_notes')
# ①의 unknown_fields와 ③④⑤가 넘기는 recheck_fields에 쓸 수 있는 이름
EXTRACTION_FIELD_NAMES = frozenset(QUOTE_FIELDS) | frozenset(ARGUMENT_FIELDS)
# 통화 표기. 단가에 붙은 외화 표기('3.20 USD', '$3.20')는 숫자와 통화로 나눠 읽는다
_CURRENCIES = {'원': 'KRW', '₩': 'KRW', 'KRW': 'KRW', '원화': 'KRW', '원(KRW)': 'KRW', 'USD': 'USD', 'US$': 'USD',
               '$': 'USD', '달러': 'USD', 'EUR': 'EUR', '€': 'EUR', '유로': 'EUR', 'JPY': 'JPY', '¥': 'JPY', '엔': 'JPY',
               'CNY': 'CNY', '위안': 'CNY'}
_NUMBER = r'(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?'
_FOREIGN_PRICE = re.compile(rf'(?P<pre>US\$|\$|€|¥|₩|USD|EUR|JPY|CNY|KRW)?\s*(?P<num>{_NUMBER})\s*'
                            r'(?P<post>USD|EUR|JPY|CNY|KRW|달러|유로|엔|위안)?')
# 금액 배수 표기. 숫자 뒤('9,700천원'·'970만원')나 표 머리글('(단위: 천원)'·'금액(천원)')에 온다.
# 머리글 패턴은 공백·천 단위 쉼표를 뺀 원문 줄(_normalized)에 쓴다
_SCALES = {'천': 1000, '만': 10_000, '백만': 1_000_000, '억': 100_000_000}
_SCALE_WORD = r'(백만|천|만|억)'
_SCALED_PRICE = re.compile(rf'\s*({_NUMBER})\s*{_SCALE_WORD}\s*(원.*)')
_SCALE_HEADER = rf'단위[:：]?\(?{_SCALE_WORD}원|\({_SCALE_WORD}원\)'
_FOREIGN_WORD = r'(?:US\$|\$|€|¥|USD|EUR|JPY|CNY|달러|유로|엔|위안)'
_CURRENCY_HEADER = rf'단위[:：]?\(?{_FOREIGN_WORD}|\({_FOREIGN_WORD}\)'
FOUND_TEXT_LIMIT = 120      # 거절 힌트(found)로 돌려주는 원문 한 줄의 길이 상한
# 단가 표기 허용 목록. 원문 단가 숫자 바로 뒤에 올 수 있는 표기: 줄 끝·구분 기호, 원·통화, 배수+원, per, 조사
_NOTATION_AFTER_OK = (r"(?:$|[^A-Za-z가-힣$€¥₩£₹₽'’千円]|원|KRW|USD|EUR|JPY|CNY|달러|유로|위안|엔(?![가-힣])"
                      rf'|{_SCALE_WORD}원|per|PER|VAT|vat|(?:으로|로|에서|에|입니다|이며|이고|부터|까지|씩|이상|이하|미만|초과)'
                      r'(?![가-힣]))')
_SPACED_UNKNOWN = r'(?:[kK](?![A-Za-z])|千|円|(?:백만|천|만|억)(?!원)|[Tt]housand)'
# 숫자 바로 앞의 통화처럼 보이는 표기('HK$', 'RMB', '£'). 아는 통화·단위 표기면 허용한다
_NOTATION_BEFORE_RUN = r"[A-Za-z$€¥₩£₹₽'’]*$"
_NOTATION_BEFORE_SUSPECT = r"[A-Z]{1,4}[$€¥₩£]?|[$€¥₩£₹₽'’]+"
_NOTATION_BEFORE_OK = {'USD', 'EUR', 'JPY', 'CNY', 'KRW', 'US$', '$', '€', '¥', '₩', 'KG', 'EA', 'BOX', 'PER'}
# 숫자 바로 뒤가 수량 단위면 그 자리는 단가가 아니다('1,000kg'). 허용 목록 검사에서 건너뛴다
_QUANTITY_AFTER = r'(?:kg|KG|Kg|g|t|톤|박스|box|Box|BOX|상자|개|EA|ea|팩|봉|포대)(?![A-Za-z])'
# 읽을 수 없는 천 단위 표기 (표 머리글의 '000·千 등)
_UNKNOWN_SCALE = r"['’]000|\(000\)|千|[Tt]housand|THOUSAND"
_UNIT_HEADER = r'[(\[]단위[:：]?(?P<a>[^)\]]*)[)\]]|단위[:：](?P<b>[^)\],|]*)'
# 배수(천원·만원)를 적용한 단가가 기준 단가(같은 메일의 인상 전 단가·같은 업체 이전 견적)와 이 배수를 넘게 차이 나면
# 거절한다. 배수 없이 원문 그대로 읽은 단가는 비교하지 않는다
PRICE_REFERENCE_RATIO = Decimal(5)
# 비용이 없다고 원문에 적힌 표기. 0원으로 저장하고 원문 구절은 그대로 남긴다 (적혀 있지 않은 비용은 0원이 아니라 미확인)
_ZERO_AMOUNTS = {'무료', '무상', '없음', '없습니다', '해당없음'}
# 단가의 뜻을 정하는 조건. 이전 버전과 값이 다르면 이전 단가·단가 구간·이 조건들을 이어받지 않는다
PRICE_BASIS = ('unit', 'currency', 'order_unit', 'quantity_per_order_unit')
# 저장하는 단위. 원문 표기는 _UNIT_ALIASES·unit_name으로 바꾸고, 이 밖의 단위는 INVALID_UNIT으로 거절한다
_UNITS = {'kg', 'g', '톤', 'box', '개', '팩', '봉', '포대'}
_UNIT_ALIASES = {'Box': 'box', '상자': 'box', '그램': 'g', 't': '톤', 'EA': '개', 'ea': '개'}
# unit_code를 받는 항목: 단위 자체와 단위가 붙는 수량
_UNIT_FIELDS = {'unit', 'order_unit', 'available_quantity', 'quantity_per_order_unit', 'moq', 'pack_multiple'}
# 수량에 붙는 수식어 ('최소 20박스', '5박스 단위', '500kg 미만', '박스당 10kg'). 숫자를 읽을 때만 떼고,
# 원문 대조는 붙인 채로 한다. 숫자 뒤의 단위는 pricing_terms_jsonb의 '{필드}_unit'으로 저장한다
_QUANTITY_FIELDS = {'available_quantity', 'quantity_per_order_unit', 'moq', 'pack_multiple'}
# 앞말은 여러 개가 겹칠 수 있다 ('월 최대 1,000kg'). 월·주·일은 공급 가능량의 기간 표현이다
_QUANTITY_WORDS = re.compile(r'^\s*(?:(?:최소\s*주문\s*량|최소\s*발주\s*량|MOQ|최소|최대|(?:매\s*)?(?:월|주|일)(?:간)?|(?:1\s*)?[가-힣A-Za-z]+\s*당)\s*:?\s*)+'
                             r'|\s*(?:이상|이하|미만|초과|단위|까지|부터)\s*$')
# 단가 구간 경계의 뜻. 최솟값에는 아래 경계 표현만, 최댓값에는 위 경계 표현만 올 수 있다
_LOWER_BOUND_WORDS = ('이상', '초과', '부터')
_UPPER_BOUND_WORDS = ('미만', '이하', '까지')
# 날짜 뒤의 시각·'출고분·주문 확정분' 같은 대상 표기와 '까지·부터·자' (예: '2026-12-01 출고분부터', '2026-11-30 18:00까지')
_DATE_SUFFIX = re.compile(r'(?:\s*\d{1,2}:\d{2})?\s*(?:(?:출고|입고|납품|주문|발주)\s*(?:확정\s*)?분\s*)?(?:까지|부터|자)?\s*$')
_DOTTED_DATE = re.compile(r'(\d{4})\s*[./]\s*(\d{1,2})\s*[./]\s*(\d{1,2})\s*\.?')   # 2026. 12. 1. / 2026/12/01
# AI·시스템에게 하는 문장으로 보이는 줄. 값을 거절하지 않고, 이런 줄이 있는 메일의 견적만 NEEDS_REVIEW로 둔다
_INSTRUCTION = re.compile(r'(?i)(?<![a-z])(?:AI|LLM|GPT|assistant|chatbot)(?![a-z])|인공지능|챗봇|어시스턴트'
                          r'|시스템\s*(?:안내|지시|메시지|프롬프트)|system\s*prompt|이\s*메일을\s*처리하는'
                          r'|지시(?:를|는)?\s*무시|ignore\s+(?:all|previous|the\s+above)')
# 글자 항목(text)의 저장 값 길이 상한. 원문에서 값 부분만 옮기면 넘지 않는 길이다. 넘으면 TOO_LONG으로 거절한다
TEXT_FIELD_LIMITS = {'supplier_name': 60, 'item_name': 60, 'specification': 120, 'origin': 60, 'certifications': 120,
                     'tax_terms': 120, 'lead_time': 120, 'increase_reason': 300}
NOTE_TEXT_LIMIT = 300       # 품목 비고 원문 한 문장
COST_TEXT_LIMIT = 300       # 비용 조건 원문 구절
# 밖으로 나가는 메일(견적 요청·보완 질문·역제안)에 넣는 업무 값의 길이 상한과 지울 주소 표기
MAIL_VALUE_LIMITS = {'품목': 60, '규격': 120, '조건': 150}
# 한 실행(②)에서 웹 검색(crawl_supplier_candidates)을 부를 수 있는 횟수
WEB_SEARCH_CALLS_PER_RUN = 1
_URL = re.compile(r'(?i)\b(?:https?://|www\.)\S+')
# 거절·조정 코드별로 LLM이 다음 호출에서 고칠 방법
_HINTS = {
    'TOO_LONG': '그 항목의 값 부분만 짧게 옮긴다. 앞뒤 문장·설명은 넣지 않는다. 품목명·규격은 원문 표기 그대로 핵심만 쓴다.',
    'SOURCE_MISMATCH': '원문 표기 그대로 값 부분만 옮긴다. 원문에 없는 값은 null로 보낸다.',
    'MISSING_EVIDENCE': '값을 읽은 메일의 evidence{mail_id, part, page}를 붙인다. 원문에서 그 항목을 직접 읽지 않았다면 '
                        '(다른 항목의 값을 옮겨 쓴 경우 등) 그 항목은 빼고 다시 보낸다.',
    'EVIDENCE_NOT_IN_CASE': 'get_case_context 메일 목록의 mail_id와 part(본문 body, 첨부 att-1 등)를 쓴다.',
    'INVALID_NUMBER': '숫자와 단위·통화만 옮긴다 (예: "54,500원", "300kg", "3.20 USD"). 앞뒤의 설명 낱말은 빼고 '
                      '다시 보낸다 (예: "박스당 중량은 10kg" → "10kg"). 그래도 안 되면 그 항목만 null로 보낸다.',
    'INVALID_DATE': '연도가 있는 날짜만 옮긴다. 연도가 없으면 그 항목만 null로 보낸다.',
    'VALID_UNTIL_BEFORE_EFFECTIVE': '유효기간이 적용일보다 이르면 단가 종료일이 아니라 회신 기한이다. valid_until은 빼고 '
                                    '그 문장을 item_notes(PRICE_NOTICE)로 다시 보낸다.',
    'INVALID_UNIT': 'kg·g·톤·박스·개·팩·봉·포대 중 원문 단위를 옮긴다. 원문 표기가 이 이름과 다르면(예: "킬로", "봉지") '
                    'raw_value는 원문 그대로 두고 unit_code에 정해진 단위를 적는다. 원문에 단위가 없으면 null로 보낸다.',
    'UNIT_CODE_MISMATCH': 'unit_code가 원문 단위와 다르다. 원문 단위를 다시 확인한다.',
    'BASIS_TYPE_MISMATCH': '비용의 basis_type·basis_unit이 basis 원문과 맞지 않는다. 주문·배송 한 번마다면 PER_ORDER, '
                           '수량 단위마다면 PER_UNIT과 그 단위(basis_unit)다. 원문에 기준이 없으면 둘 다 보내지 않는다.',
    'UNKNOWN_FIELD': 'fields에는 정해진 field_name만 쓴다. 비용·단가 구간·비고는 cost_terms·price_tiers·item_notes 인자로 보낸다.',
    'DUPLICATE_FIELD': '같은 field_name은 한 번만 보낸다.',
    'TIER_BOUND_MISPLACED': '구간마다 "이상·초과·부터" 경계는 min_quantity, "미만·이하·까지" 경계는 max_quantity에 나눠 넣는다.',
    'TIER_OVERLAP': '단가 구간의 경계를 원문에서 다시 확인한다.',
    'CURRENCY_MISMATCH': '단가의 통화 표기와 currency가 다르다. 원문을 다시 확인한다.',
    # 아래 단가 코드들은 rejected 항목에 sent(보낸 값)·found(문제를 찾은 원문 줄 {mail_id, part, page, line, text})가 붙는다
    'PRICE_SCALE_MISSING': '원문 found 줄에 천원·만원 같은 배수 표기가 있다. 숫자 뒤에 붙어 있으면 단가를 "9,700천원"처럼 '
                           '배수까지 옮기고, 표 머리글에만 있으면(예: "(단위: 천원)") 단가는 숫자 그대로 두고 '
                           'price_scale_text에 그 머리글 구절을 넣어 다시 보낸다.',
    'PRICE_SCALE_INVALID': 'price_scale_text는 단가(unit_price·previous_unit_price·price_tiers)에만, 원문의 배수 구절'
                           '(예: "단위: 천원")을 그대로 넣는다. 단가에 원·통화·배수가 이미 적혀 있으면 보내지 않는다.',
    'CURRENCY_MISSING': '원문 found 줄에 외화 표기가 있다. 단가를 "USD 3.10"처럼 통화까지 옮기거나, 통화가 표 머리글에만 '
                        '있으면 currency에 그 통화를 넣어 다시 보낸다.',
    'PRICE_NOTATION_UNKNOWN': '원문 found 줄의 단가 표기(숫자 앞뒤의 배수·통화·머리글)를 도구가 읽을 수 없다. 원·통화·천원·만원 '
                              '중 그 뜻에 맞는 표기로 확인할 수 없으면 그 단가만 null로 보낸다(값을 짐작해 바꾸지 않는다).',
    'PRICE_IMPLAUSIBLE': '배수(천원·만원)를 적용한 단가가 기준 단가(reference: 같은 메일의 인상 전 단가 또는 이 업체의 이전 '
                         '견적)와 5배 넘게 차이 난다. 그 배수가 이 단가에 적용되는 표기인지 found 줄에서 다시 확인한다. '
                         '다른 표·문장의 배수면 price_scale_text를 빼고, 확인할 수 없으면 그 단가만 null로 보낸다.',
    'PRICE_UNIT_MISMATCH': '단가의 단위는 원문에서 단가 바로 뒤("54,500원/박스" → "박스")나 같은 줄·단위 머리글'
                           '("원/kg"·"kg당")에 적힌 단위다. "박스당 10kg" 같은 중량은 quantity_per_order_unit으로 보낸다.',
    'MISSING_FIELD': '업체가 연결되지 않은 메일이면 원문의 업체명을 supplier_name으로 보낸다. 원문에 없으면 HOLD한다.',
    'SUPPLIER_NOT_MATCHED': '등록된 업체명과 글자가 정확히 같지 않다. 결과의 next를 따른다.',
    'SUPPLIER_MISMATCH': '메일에 연결된 업체와 업체명이 다르다. 저장하지 말고 HOLD한다.',
    'SUPPLIER_EVIDENCE_MIXED': '한 업체의 메일 근거만 남기고 다시 보낸다.',
    'SUPPLY_QUANTITY_MISSING': '인상 통보 메일에는 공급 물량이 있어야 한다. 같은 메일의 본문·첨부 중 아직 읽지 않은 '
                               '부분이 있으면 먼저 읽는다. 공급 계획·공급 물량·공급 가능량이라고 적힌 '
                               '수량만 단위와 함께 available_quantity로 다시 보낸다. 규격·포장·주문 단위의 수량'
                               '(예: 10kg/박스, 박스당 10kg, 주문 단위 10kg 박스)은 공급 물량이 아니다. '
                               '그런 수량만 있으면 다시 보내지 말고 HOLD한다.',
    'TAX_BASIS_NOT_TAX_TERMS': 'tax_basis는 field_name이 tax_terms인 항목에만 붙인다.',
    'UNIT_CODE_NOT_UNIT_FIELD': 'unit_code는 unit·order_unit과 단위가 붙는 수량 항목에만 붙인다.',
    'INSTRUCTION_NOT_NOTE': 'AI·시스템에게 하는 문장이라 비고에서 뺐다. 다시 보내지 않는다.',
}


class EvidenceRef(StrictModel):
    mail_id: str                       # mail_messages.id
    part: str = 'body'                 # 'body' 또는 첨부 attachment_ref
    page: int | None = Field(default=None, ge=1)
    cell: str | None = None            # 표의 행·열 등


UnitCode = Literal['kg', 'g', '톤', 'box', '개', '팩', '봉', '포대']


class QuoteField(StrictModel):
    field_name: str
    raw_value: str | None              # 원문에 적힌 그대로. 원문에 없으면 null
    evidence: EvidenceRef | None = None
    # field_name이 tax_terms일 때만: 원문이 말하는 부가세 기준 (포함·별도·면세). 판단할 수 없으면 null
    tax_basis: Literal['INCLUDED', 'EXCLUDED', 'EXEMPT'] | None = None
    # 단위가 있는 항목(unit·order_unit·수량)에서 원문 단위 표기가 정해진 이름과 다를 때 ①이 고른 단위 ('킬로' → kg)
    unit_code: UnitCode | None = None
    # 단가 항목만: 배수가 표 머리글에만 있을 때 그 원문 구절 ('(단위: 천원)'). 숫자 뒤에 붙은 배수는 raw_value에 둔다
    price_scale_text: str | None = None


class PriceTier(StrictModel):
    min_quantity: str | None = None    # 원문 그대로
    max_quantity: str | None = None
    unit_price: str
    evidence: EvidenceRef
    price_scale_text: str | None = None  # QuoteField와 같다


class CostTerm(StrictModel):
    cost_type: Literal['SHIPPING', 'HANDLING', 'OTHER']
    source_text: str = Field(min_length=1)  # 이 비용 조건이 적힌 원문 구절 그대로
    amount: str | None = None          # 원문 그대로. 무료 근거가 있으면 '무료'
    basis: str | None = None           # 산정 기준 원문 (예: 회당, 박스당)
    # 산정 기준의 뜻 (①이 고른다): 주문·배송 한 번마다(PER_ORDER) / 수량 단위마다(PER_UNIT, basis_unit 필수).
    # 계산은 이 값을 쓰고, 없으면(이전 견적) basis 원문을 키워드로 읽는다
    basis_type: Literal['PER_ORDER', 'PER_UNIT'] | None = None
    basis_unit: UnitCode | None = None
    payer: str | None = None           # 부담 주체 원문
    included_in_price: StrictBool | None  # 비용마다 정한다: 포함 true, 별도 청구 false, 원문에 없으면 null
    evidence: EvidenceRef


class ItemNote(StrictModel):
    text: str = Field(min_length=1)    # 품목 비고 원문 그대로
    # 입고·품질·포장·결제·공급·가격 예고. 어느 분류에도 맞지 않는 문장(인사·안내·회사 소개)은 비고가 아니다
    category: Literal['RECEIVING', 'QUALITY', 'PACKING', 'PAYMENT', 'SUPPLY', 'PRICE_NOTICE']
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
    currency: str = 'KRW'              # 원문에 통화가 없으면 KRW (MOQ·발주 배수는 pricing_terms_jsonb에 있다)
    mail_normalized: dict              # mail_id → mail_messages.normalized_jsonb
    case_conditions: dict | None       # 비어 있는 work_cases 조건만: item_name·specification·standardized_item_key
                                       # (최초 추출), quantity·unit(인상 통보의 공급 물량)
    supplier_item_id: str | None = None  # 같은 품목으로 확인된 그 업체의 등록 품목(supplier_items.id)


class QuoteStore(CaseStore, Protocol):
    """store_structured_mail의 저장소 (06 suppliers·quote_versions·mail_messages·work_cases)."""

    def resolve_supplier(self, case_id: str, supplier_name: str) -> str | None:
        """suppliers.name이 정확히 같은 업체 하나의 id. 없거나 여럿이면 None. 거래처를 새로 만들지 않는다.
        견적 업체는 메일의 mail_messages.supplier_id로 정하고, 이 조회는 연결이 없는 메일과 업체명 대조에만 쓴다."""

    def latest_quote_version(self, case_id: str, supplier_id: str) -> tuple[str, QuoteVersionRecord] | None:
        """이 업무·업체의 최신 quote_versions (id, 행)."""

    def list_supplier_items(self, case_id: str, supplier_id: str) -> list[dict]:
        """그 업체의 등록 품목(supplier_items) {id, item_name, specification, unit, standardized_item_key}."""

    def record_extraction_issue(self, case_id: str, agent_run_id: str, issue: dict) -> None:
        """현재 ① 실행의 업체 거절(미등록·불일치)을 기록한다. issue는 {code, field, supplier_name, evidence:[{evidence_id, excerpt}]}.
        서버가 근거 원문과 다시 대조하고 보류 사유로 쓴다."""

    def insert_quote_version(self, record: QuoteVersionRecord,
                             expected_input_revision: int) -> tuple[str, QuoteVersionRecord] | None:
        """quote_versions·price_history·mail_messages.normalized_jsonb·work_cases 조건·workflow_logs를 한 트랜잭션으로
        저장하고 실제로 저장된 (quote_versions.id, 행)을 돌려준다. input_revision은 올리지 않는다.
        revision이 다르면 저장하지 않고 None.
        같은 원문 메일·같은 견적 내용(version_no·supersedes_id·case_conditions 제외)이면 새로 저장하지 않고 기존 행을 돌려준다.
        normalized_jsonb는 덮어쓰지 않는다. fields는 필드별로 새 값으로 바꾸고 item_notes는 없던 것만 더하며 다른 키는 둔다."""


_quote_store: QuoteStore | None = None


def use_quote_store(store: QuoteStore | None) -> None:
    global _quote_store
    _quote_store = store


def _nfkc(text: str) -> str:
    """글자 모양만 다른 표기를 한 가지로 ('ｋｇ'·'㎏' → 'kg', '１,０００' → '1,000'). 뜻은 바꾸지 않는다."""
    return unicodedata.normalize('NFKC', text)


def _compact(text: str) -> str:
    return re.sub(r'\s+', '', _nfkc(text))


def _quantity_text(raw: str) -> str:
    return _QUANTITY_WORDS.sub('', _nfkc(raw).strip()).strip()


def _date(raw: str, field_name: str) -> str:
    """연도가 있는 날짜만 받는다. '2026년 12월 1일', '2026-12-01', '2026. 12. 1.', 뒤의 '까지·부터'는 허용."""
    text = _DATE_SUFFIX.sub('', _nfkc(raw).strip())
    match = _DOTTED_DATE.fullmatch(text)
    if match is None:
        return explicit_date(text, field_name).isoformat()
    try:
        return date(*map(int, match.groups())).isoformat()
    except ValueError:
        raise ReviewRequired('INVALID_DATE', f'{field_name}의 날짜를 확인할 수 없습니다.', field_name)


def _unit(raw: str, field_name: str, code: str | None = None) -> str:
    """'kg', '박스', '원/kg', 'kg당', '1kg'을 저장 단위로 바꾼다. 정해진 단위가 아니면 INVALID_UNIT.
    code는 ①이 원문 단위를 정해진 단위 중 하나로 고른 값(unit_code)이다. 표기를 코드가 못 읽을 때('킬로', '봉지')
    그 값을 쓰고, 코드가 읽은 단위와 다르면 UNIT_CODE_MISMATCH로 거절한다."""
    text = re.sub(r'\s*당$', '', re.sub(r'^(?:원\s*)?/\s*', '', _nfkc(raw).strip()))
    text = re.sub(r'^1\s*(?=\D)', '', text)          # '1kg 당 단가'의 1kg
    unit = unit_name(_UNIT_ALIASES.get(text, text))
    if unit not in _UNITS:
        if code in _UNITS:
            return code
        raise ReviewRequired('INVALID_UNIT', f'{field_name}의 단위를 확인할 수 없습니다.', field_name)
    if code is not None and code != unit:
        raise ReviewRequired('UNIT_CODE_MISMATCH', f'{field_name}의 단위 표기와 unit_code가 다릅니다.', field_name)
    return unit


def _price(raw: str, field_name: str) -> tuple[str, str | None]:
    """단가·금액을 (소수 문자열, 외화 통화 코드)로 읽는다. '3.20 USD', 'USD 3.10', '$3.20'은 통화를 나누고,
    그 밖에는 원화 표기만 받는다. '9,700천원'·'970만원'은 배수를 곱한다."""
    raw = _nfkc(raw)
    if scaled := _SCALED_PRICE.fullmatch(raw):
        return _scaled(str(decimal_value(scaled[1] + scaled[3], field_name)), _SCALES[scaled[2]]), None
    match = _FOREIGN_PRICE.fullmatch(raw.strip())
    if match and (match['pre'] or match['post']):
        return str(decimal_value(match['num'], field_name)), _CURRENCIES[match['pre'] or match['post']]
    return str(decimal_value(raw, field_name)), None


def _quantity(raw: str, field_name: str, code: str | None = None) -> tuple[str, str | None]:
    """수량을 (소수 문자열, 단위)로 읽는다. '최소 20박스' → ('20', 'box'), '박스당 10kg' → ('10', 'kg'), '300' → ('300', None).
    code는 _unit과 같다. 단위 표기가 없으면 쓰지 않는다."""
    match = re.fullmatch(rf'({_NUMBER})\s*(.*)', _quantity_text(raw))
    if match is None:
        raise ReviewRequired('INVALID_NUMBER', f'{field_name}의 숫자와 단위를 확인할 수 없습니다.', field_name)
    try:
        unit = _unit(match[2], field_name, code) if match[2] else None
    except ReviewRequired as error:
        if error.issue['code'] == 'UNIT_CODE_MISMATCH':
            raise
        raise ReviewRequired('INVALID_NUMBER', f'{field_name}의 숫자와 단위를 확인할 수 없습니다.', field_name)
    return str(decimal_value(match[1], field_name)), unit


def _parse(kind: str, field_name: str, raw: str, unit_code: str | None = None) -> str:
    """원문 값을 저장 값으로 바꾼다. 숫자는 소수 문자열, 날짜는 ISO, 단위·통화는 표준 이름."""
    if kind == 'price':
        return _price(raw, field_name)[0]
    if kind == 'number':
        return (_quantity(raw, field_name, unit_code)[0] if field_name in _QUANTITY_FIELDS
                else str(decimal_value(_nfkc(raw), field_name)))
    if kind == 'date':
        return _date(raw, field_name)
    if kind == 'unit':
        return _unit(raw, field_name, unit_code)
    if kind == 'currency':
        return _CURRENCIES.get(raw.strip(), raw.strip().upper())
    return ' '.join(raw.split())        # 글자 항목: 표의 줄바꿈·여러 공백은 한 칸으로 (원문 대조는 raw_value로 한다)


def _normalized(text: str, numeric: bool) -> str:
    """공백을 없애고, 숫자면 천 단위 쉼표(1,000kg와 1000kg)도 없앤다."""
    text = _compact(text)
    return re.sub(r'(?<=\d),(?=\d)', '', text) if numeric else text


def _contains(text: str, value: str, numeric: bool) -> bool:
    """numeric이면 더 긴 숫자의 일부는 같은 값으로 보지 않는다 ('0'은 '10,200'에, '800원'은 '9,800원'에 없다)."""
    if not numeric:
        return value in text
    pattern = re.escape(value)
    if value[:1].isdigit():
        pattern = r'(?<![\d.])' + pattern
    if value[-1:].isdigit():
        pattern += r'(?!\.?\d)'
    return re.search(pattern, text) is not None


# 저장소가 근거 메일을 범위 밖·없음으로 거절하는 코드. 거절 사유로 바꾸고, 다른 오류(장애)는 그대로 올린다
_OUT_OF_SCOPE = {'ACTION_NOT_ALLOWED', 'RESOURCE_NOT_FOUND'}


class _SourceCheck:
    """근거가 이 업무의 메일·첨부 쪽이고 값이 그 쪽 원문에 그대로 있는지 확인한다. 쪽 원문은 한 번만 읽는다.
    읽은 쪽에 AI·시스템에게 하는 문장이 있으면 그 메일을 instruction_mails에 모은다."""

    def __init__(self, store: CaseStore, case_id: str):
        self.store, self.case_id, self.parts, self.instruction_mails = store, case_id, {}, set()

    def _part(self, evidence: EvidenceRef) -> dict | None:
        key = (evidence.mail_id, evidence.part, evidence.page)
        if key not in self.parts:
            part = self.parts[key] = self._read_part(key)
            if part and any(_INSTRUCTION.search(line) for line in part['text'].splitlines()):
                self.instruction_mails.add(evidence.mail_id)
        return self.parts[key]

    def _read_part(self, key: tuple) -> dict | None:
        """근거 쪽 원문. 저장소가 이번 실행 범위 밖이거나 없는 메일이라고 거절하면 None(EVIDENCE_NOT_IN_CASE 거절)으로 본다.
        서버 저장소는 범위 밖 메일에 예외를 올린다. 그대로 두면 근거 메일 ID 하나가 틀려도 실행 전체가 실패한다."""
        try:
            return _read(lambda: self.store.read_mail_part(self.case_id, *key))
        except ServiceError as error:
            if error.code in _OUT_OF_SCOPE:
                return None
            raise

    def lines(self, evidence: EvidenceRef, raw: str, numeric=False) -> list[str]:
        """값이 들어 있는 원문 줄. 값이 여러 줄에 걸쳐 있으면 빈 목록."""
        value = _normalized(raw, numeric)
        return [line for line in self._part(evidence)['text'].splitlines()
                if _contains(_normalized(line, numeric), value, numeric)]

    def __call__(self, evidence: EvidenceRef | None, raw: str, numeric=False) -> tuple[str | None, dict | None]:
        if evidence is None:
            return 'MISSING_EVIDENCE', None
        part = self._part(evidence)
        if part is None:
            return 'EVIDENCE_NOT_IN_CASE', None
        if not _contains(_normalized(part['text'], numeric), _normalized(raw, numeric), numeric):
            return 'SOURCE_MISMATCH', None
        location = ' '.join(filter(None, [part.get('location'), evidence.cell]))
        return None, {'mail_id': evidence.mail_id, 'location': location}

    def find(self, evidence: EvidenceRef, pattern: str, anchor: str | None = None) -> tuple[dict, re.Match] | None:
        """pattern이 맞는 첫 원문 줄(공백·천 단위 쉼표를 뺀 줄에서 찾는다)의 위치와 매치. 위치는 거절 힌트 found로 쓴다.
        anchor(단가 숫자 패턴)를 주면 그 첫 줄과 그 위로 빈 줄 전까지(같은 표·문단 덩어리)에서만 찾는다.
        같은 쪽의 다른 표 머리글('(단위: 천원)')을 단가에 적용하지 않기 위해서다.
        AI·시스템에게 하는 문장으로 보이는 줄은 위치만 주고 글은 주지 않는다 (LLM에게 다시 전달되지 않게)."""
        lines = self.lines_of(evidence)
        rows = range(len(lines))
        if anchor is not None:
            at = next((i for i, line in enumerate(lines) if re.search(anchor, _normalized(line, True))), None)
            if at is None:
                return None
            rows = range(next((i + 1 for i in range(at - 1, -1, -1) if not lines[i].strip()), 0), at + 1)
        for index in rows:
            if match := re.search(pattern, _normalized(lines[index], True)):
                return self.located(evidence, index + 1, lines[index]), match
        return None

    def lines_of(self, evidence: EvidenceRef) -> list[str]:
        return (self._part(evidence) or {}).get('text', '').splitlines()

    @staticmethod
    def located(evidence: EvidenceRef, line_no: int, line: str) -> dict:
        found = {'mail_id': evidence.mail_id, 'part': evidence.part, 'page': evidence.page, 'line': line_no}
        if not _INSTRUCTION.search(line):
            found['text'] = ' '.join(line.split())[:FOUND_TEXT_LIMIT]
        return found


def _scaled(value: str, scale: int) -> str:
    return format(Decimal(value) * scale, 'f')


def _number_pattern(raw: str) -> str:
    """단가 표기의 숫자를 정규화한 원문 줄에서 찾는 패턴. 더 긴 숫자의 일부는 맞지 않는다."""
    number = re.search(_NUMBER, _nfkc(raw))[0].replace(',', '')
    return rf'(?<![\d.]){re.escape(number)}(?!\.?\d)'


def _known_header(token: str) -> bool:
    """'(단위: …)' 머리글 값을 읽을 수 있는지. 금액 표기(원·천원·통화)나 정해진 단위, 그 둘의 '/' 조합('천원/박스')만 안다."""
    def known(part: str) -> bool:
        if re.fullmatch(rf'{_SCALE_WORD}?원|KRW|₩|{_FOREIGN_WORD}', part):
            return True
        try:
            _unit(part, 'unit')
            return True
        except ReviewRequired:
            return False
    return all(known(part) for part in token.split('/') if part) and bool(token.strip('/'))


def _notation_issue(check: _SourceCheck, evidence: EvidenceRef, raw: str, number: str) -> dict | None:
    """원문에서 단가 숫자가 나오는 자리마다 바로 앞뒤 표기가 아는 표기인지 (허용 목록).
    모르는 표기가 한 자리라도 있으면 PRICE_NOTATION_UNKNOWN. 목록에 없는 배수·통화 표기('9.7k', '9,700천', 'HK$',
    '千円')가 숫자만 읽혀 저장되지 않게 한다. 숫자 뒤가 수량 단위인 자리('1,000kg')는 단가가 아니라 건너뛴다."""
    for index, line in enumerate(check.lines_of(evidence)):
        # 공백은 남긴다: 표의 칸 사이 공백·탭 뒤 낱말('9,700\t냉동딸기')은 표기가 아니다
        text = re.sub(r'(?<=\d),(?=\d)', '', _nfkc(line))
        for match in re.finditer(number, text):
            after = text[match.end():]
            if re.match(_QUANTITY_AFTER, after.lstrip()):
                continue
            # 붙어 있으면 허용 목록, 띄어 있으면 배수처럼 보이는 낱말만 거절한다('9,700 k', '9,700 천')
            bad_after = re.match(_SPACED_UNKNOWN, after.lstrip()) if after[:1].isspace() \
                else not re.match(_NOTATION_AFTER_OK, after)
            before = re.search(_NOTATION_BEFORE_RUN, text[:match.start()].rstrip())[0]
            suspect = re.fullmatch(_NOTATION_BEFORE_SUSPECT, before) and before.upper() not in _NOTATION_BEFORE_OK
            if suspect or bad_after:
                return {'code': 'PRICE_NOTATION_UNKNOWN', 'sent': raw,
                        'found': check.located(evidence, index + 1, line)}
    return None


def _price_issue(check: _SourceCheck, evidence: EvidenceRef, raw: str, scale_text: str | None,
                 foreign_given: bool) -> tuple[int, dict | None]:
    """단가의 배수·통화 표기를 원문과 대조한다. 원문 대조를 통과한 단가에만 부른다. 반환: (곱할 배수, 거절 항목).
    1) 원문 단가 줄에서 숫자 앞뒤 표기가 아는 표기여야 한다(허용 목록).
    2) raw에 원·통화·배수가 적혀 있으면 원문 대조로 확인된 표기다.
    3) 숫자만 왔는데 원문에서 그 숫자 바로 뒤에 '천원'·통화가 붙어 있거나, 같은 표 덩어리에 '(단위: 천원)'·'(단위: USD)'
       머리글이 있으면 거절한다. 읽을 수 없는 '단위' 머리글이나 천 단위 표기('000·千)가 있으면 PRICE_NOTATION_UNKNOWN.
    머리글은 단가 줄과 그 위로 빈 줄 전까지만 본다. price_scale_text도 그 범위에 있어야 한다.
    foreign_given이면(currency로 외화를 따로 보냄) 통화 표기는 확인된 것으로 본다."""
    number = _number_pattern(raw)
    if problem := _notation_issue(check, evidence, raw, number):
        return 1, problem
    marked = re.search(rf'원|₩|KRW|{_FOREIGN_WORD}', _nfkc(raw))
    if scale_text:
        word = re.search(rf'{_SCALE_WORD}\s*원', _nfkc(scale_text))
        if marked or word is None or not check.find(evidence, re.escape(_normalized(scale_text, True)), number):
            return 1, {'code': 'PRICE_SCALE_INVALID', 'sent': scale_text}
        return _SCALES[word[1]], None
    if marked:
        return 1, None
    checks = [('PRICE_SCALE_MISSING', rf'{number}{_SCALE_WORD}원', None),
              ('PRICE_SCALE_MISSING', _SCALE_HEADER, number)]
    if not foreign_given:
        checks += [('CURRENCY_MISSING', rf'{_FOREIGN_WORD}{number}|{number}{_FOREIGN_WORD}', None),
                   ('CURRENCY_MISSING', _CURRENCY_HEADER, number)]
    checks += [('PRICE_NOTATION_UNKNOWN', _UNKNOWN_SCALE, number)]
    for code, pattern, anchor in checks:
        if hit := check.find(evidence, pattern, anchor):
            return 1, {'code': code, 'sent': raw, 'found': hit[0]}
    header = check.find(evidence, _UNIT_HEADER, number)
    if header and not _known_header(re.split(r'[,，]', header[1]['a'] if header[1]['a'] is not None
                                             else header[1]['b'])[0]):
        return 1, {'code': 'PRICE_NOTATION_UNKNOWN', 'sent': raw, 'found': header[0]}
    return 1, None


def _implausible(value: str, reference: str | None) -> bool:
    """기준 단가와 PRICE_REFERENCE_RATIO배 넘게 차이 나는지. 기준이 없거나 0 이하면 비교하지 않는다."""
    if reference is None or Decimal(reference) <= 0 or Decimal(value) <= 0:
        return False
    ratio = Decimal(value) / Decimal(reference)
    return not 1 / PRICE_REFERENCE_RATIO <= ratio <= PRICE_REFERENCE_RATIO


def _reference_price(values: dict, previous: tuple[str, 'QuoteVersionRecord'] | None) -> dict | None:
    """기준 단가 {value, source}. 같은 메일의 인상 전 단가, 없으면 이 업체 이전 견적의 단가(단위·통화가 같을 때만.
    이번 메일에 단위·통화가 없으면 이전 견적 것을 이어받으므로 같다고 본다). 둘 다 없으면 None(처음 받은 견적)."""
    if 'previous_unit_price' in values:
        return {'value': values['previous_unit_price']['value'], 'source': 'previous_unit_price'}
    if previous is None or previous[1].unit_price is None:
        return None
    record = previous[1]
    if values.get('unit', {}).get('value', record.unit) != record.unit or \
            values.get('currency', {}).get('value', record.currency) != record.currency:
        return None
    return {'value': record.unit_price, 'source': f'quote_versions:{previous[0]}'}


def _scale_applied(raw: str, entry: dict) -> bool:
    """배수를 곱해 읽은 단가인지 ('970만원', price_scale_text로 머리글 배수 적용)."""
    return 'price_scale_text' in entry or _SCALED_PRICE.fullmatch(_nfkc(raw)) is not None


def _implausible_prices(check: _SourceCheck, values: dict, refs: dict, tiers: list[dict],
                        sent_tiers: list['PriceTier'], previous) -> list[dict]:
    """배수를 적용한 단가·구간 단가 중 기준 단가와 PRICE_REFERENCE_RATIO배 넘게 차이 나는 것 (PRICE_IMPLAUSIBLE).
    배수 없이 원문 그대로 읽은 단가는 비교하지 않는다. 실제로 크게 오른 단가를 막지 않기 위해서다."""
    reference = _reference_price(values, previous)
    if reference is None:
        return []
    out = []
    unit_price = values.get('unit_price')
    candidates = ([('unit_price', unit_price['value'], unit_price['raw_value'], refs.get('unit_price'))]
                  if unit_price and _scale_applied(unit_price['raw_value'], unit_price) else []) + \
        [(f'price_tiers[{index}]', tier['unit_price'], tier['unit_price_raw'],
          next((sent.evidence for sent in sent_tiers if sent.unit_price == tier['unit_price_raw']), None))
         for index, tier in enumerate(tiers) if _scale_applied(tier['unit_price_raw'], tier)]
    for field, value, raw, evidence in candidates:
        if _implausible(value, reference['value']):
            hit = check.find(evidence, _number_pattern(raw)) if evidence else None
            out.append({'field': field, 'code': 'PRICE_IMPLAUSIBLE', 'sent': raw, 'value': value,
                        'reference': reference, **({'found': hit[0]} if hit else {})})
    return out


def _attached_unit(check: _SourceCheck, evidence: EvidenceRef, raw: str) -> tuple[str, dict] | None:
    """원문에서 단가 바로 뒤에 붙은 단위와 그 줄 ('54,500원/박스' → 'box', 'USD 3.10 per kg' → 'kg').
    정해진 단위가 아니면 None."""
    hit = check.find(evidence, rf'{_number_pattern(raw)}{_SCALE_WORD}?(?:원|KRW|{_FOREIGN_WORD})?(?:/|per|PER)'
                               r'(1?[가-힣A-Za-z]+)')
    if hit is None:
        return None
    try:
        return _unit(hit[1].groups()[-1], 'unit'), hit[0]
    except ReviewRequired:
        return None


def _unit_pair_issue(check: _SourceCheck, price_ref: EvidenceRef, price_raw: str, unit_ref: EvidenceRef,
                     unit: dict) -> dict | None:
    """단가와 단위가 짝인지. 원문에서 단가 바로 뒤에 단위가 붙어 있으면('54,500원/박스') 그 단위여야 하고,
    붙어 있지 않으면 단위가 단가와 같은 줄이나 단위 머리글('원/kg'·'kg당'·'단위: kg')에 있어야 한다.
    숫자 바로 뒤의 단위('박스당 10kg'의 kg)는 수량이라 단가의 단위로 보지 않는다."""
    attached = _attached_unit(check, price_ref, price_raw)
    if attached:
        return None if attached[0] == unit['value'] else \
            {'code': 'PRICE_UNIT_MISMATCH', 'sent': unit['raw_value'], 'found': attached[1]}
    core = re.sub(r'^1(?=\D)', '', re.sub(r'당$', '', re.sub(r'^(?:원)?/', '', _normalized(unit['raw_value'], True))))
    word, number = rf'(?<![\d.])1?{re.escape(core)}', _number_pattern(price_raw)
    header = rf'/1?{re.escape(core)}|{word}당|단위[:：]?\(?{re.escape(core)}'
    if check.find(price_ref, rf'{number}.*{word}|{word}.*{number}') or check.find(price_ref, header, number) \
            or check.find(unit_ref, header):
        return None
    line = check.find(price_ref, number)
    return {'code': 'PRICE_UNIT_MISMATCH', 'sent': unit['raw_value'], **({'found': line[0]} if line else {})}


def _number(check, raw: str | None, name: str, evidence: EvidenceRef, zero_words=False, quantity=False):
    """(값, 사유 코드). 원문에 없으면 (None, None). quantity면 '이상·미만' 같은 수식어를 떼고 읽는다.
    zero_words면 '무료·없음'처럼 비용이 없다고 적힌 표기를 0으로 읽는다."""
    if raw is None or not raw.strip():
        return None, None
    zero = zero_words and _compact(raw) in _ZERO_AMOUNTS
    issue, _ = check(evidence, raw, numeric=not zero)
    if issue:
        return None, issue
    if zero:
        return '0', None
    try:
        return (_quantity(raw, name) if quantity else _price(raw, name))[0], None
    except ReviewRequired as error:
        return None, error.issue['code']


def _misplaced(low: str | None, high: str | None) -> bool:
    """'500kg 미만'이 최솟값에 오거나 '1,000kg 이상'이 최댓값에 오면 뜻이 반대가 된다."""
    return any(word in (low or '') for word in _UPPER_BOUND_WORDS) or \
        any(word in (high or '') for word in _LOWER_BOUND_WORDS)


def _tiers(check, tiers: list[PriceTier], rejected: list, adjusted: list, foreign_given: bool,
           unit: dict | None) -> list[dict]:
    """단가 구간. 구간 단가도 단가와 같이 배수·통화·붙은 단위를 원문과 대조한다.
    구간마다 currency(단가에 적힌 외화, 없으면 None — 견적 통화는 save_structured_quote가 채운다)를 남긴다."""
    out = []
    for index, tier in enumerate(tiers):
        tier_values, issue = {}, None
        if _misplaced(tier.min_quantity, tier.max_quantity):
            # 경계 표현이 칸의 뜻과 반대다('500kg 미만'이 최솟값). 원문 표기대로 다시 나눠 보내게 한다
            rejected.append({'field': f'price_tiers[{index}]', 'code': 'TIER_BOUND_MISPLACED'})
            continue
        for name in ('unit_price', 'min_quantity', 'max_quantity'):
            tier_values[name], issue = _number(check, getattr(tier, name), name, tier.evidence,
                                               quantity=name != 'unit_price')
            if issue:
                break
        if issue:
            rejected.append({'field': f'price_tiers[{index}]', 'code': issue})
            continue
        scale, problem = _price_issue(check, tier.evidence, tier.unit_price, tier.price_scale_text, foreign_given)
        attached = _attached_unit(check, tier.evidence, tier.unit_price) if unit and not problem else None
        if attached and attached[0] != unit['value']:
            problem = {'code': 'PRICE_UNIT_MISMATCH', 'sent': tier.unit_price, 'found': attached[1]}
        if problem:
            rejected.append({'field': f'price_tiers[{index}]', **problem})
            continue
        if scale != 1:
            tier_values |= {'unit_price': _scaled(tier_values['unit_price'], scale),
                            'price_scale_text': tier.price_scale_text}
        # 경계의 이상·미만 같은 뜻은 원문 표기로 남긴다
        raw = {'min_quantity_raw': tier.min_quantity, 'max_quantity_raw': tier.max_quantity,
               'unit_price_raw': tier.unit_price, 'currency': _price(tier.unit_price, 'unit_price')[1]}
        out.append(tier_values | raw | check(tier.evidence, tier.unit_price, numeric=True)[1])
    bounded = sorted((t for t in out if t['min_quantity'] is not None), key=lambda t: Decimal(t['min_quantity']))
    for before, after in zip(bounded, bounded[1:]):
        if before['max_quantity'] is not None and Decimal(after['min_quantity']) < Decimal(before['max_quantity']):
            rejected.append({'field': 'price_tiers', 'code': 'TIER_OVERLAP'})
            break
    return out


def _costs(check, costs: list[CostTerm], rejected: list, review: list) -> list[dict]:
    out = []
    for index, cost in enumerate(costs):
        issue, source = check(cost.evidence, cost.source_text)
        if issue is None and len(cost.source_text) > COST_TEXT_LIMIT:
            issue = 'TOO_LONG'
        amount_text, basis = cost.amount, cost.basis
        for text, numeric in ((amount_text, amount_text and _compact(amount_text) not in _ZERO_AMOUNTS),
                              (basis, False), (cost.payer, False)):
            if not issue and text and not _contains(_normalized(cost.source_text, numeric), _normalized(text, numeric),
                                                    numeric):
                issue = 'SOURCE_MISMATCH'
        amount = None
        if not issue:
            amount, issue = _number(check, amount_text, 'amount', cost.evidence, zero_words=True)
        if not issue:
            issue = _basis_issue(cost)
        if issue:
            rejected.append({'field': f'cost_terms[{index}]', 'code': issue})
            continue
        out.append({'cost_type': cost.cost_type, 'source_text': cost.source_text, 'amount': amount, 'basis': basis,
                    'payer': cost.payer, 'included_in_price': cost.included_in_price, **source}
                   | ({'basis_type': cost.basis_type, 'basis_unit': cost.basis_unit} if cost.basis_type else {}))
    return out


def _basis_issue(cost: CostTerm) -> str | None:
    """①이 고른 산정 기준(basis_type·basis_unit)이 basis 원문과 맞는지. 뜻은 ①이 정하고, 코드는 원문에 단위가 보이는
    경우만 대조한다('박스당'을 PER_ORDER로, 'kg당'을 PER_UNIT box로 보내면 거절). 맞지 않으면 BASIS_TYPE_MISMATCH."""
    if cost.basis_type is None:
        return 'BASIS_TYPE_MISMATCH' if cost.basis_unit else None
    if cost.basis_type == 'PER_ORDER':
        unit = _unit_key(_PER_UNIT_PREFIX.sub('', cost.basis or ''))
        return 'BASIS_TYPE_MISMATCH' if cost.basis_unit or unit in _UNITS else None
    if not cost.basis or cost.basis_unit is None:
        return 'BASIS_TYPE_MISMATCH'
    unit = _unit_key(_PER_UNIT_PREFIX.sub('', cost.basis))
    return 'BASIS_TYPE_MISMATCH' if unit in _UNITS and unit != cost.basis_unit else None


def _quote_supplier(store: QuoteStore, case_id: str, case_mails: dict, evidence_mails: set,
                    supplier_name: str | None) -> tuple[str | None, dict | None, str | None]:
    """견적 업체는 근거 메일의 서버 연결(mail_messages.supplier_id)로 정한다. LLM이 적은 업체명은 대조에만 쓴다.
    반환: (supplier_id, 거절 사유, 등록 업체와 맞은 업체명 — 법인 표기를 뺐으면 뺀 이름)."""
    if not evidence_mails:
        return None, {'field': 'supplier_name', 'code': 'MISSING_FIELD'}, None
    linked = {(case_mails.get(mail_id) or {}).get('supplier_id') for mail_id in evidence_mails}
    named, matched_name = _resolve_supplier_name(store, case_id, supplier_name)
    if None in linked:
        # 업체가 연결되지 않은 메일은 업체명으로 찾되, 다른 메일 값과 섞이지 않게 한 메일 안의 근거만 묶는다
        if len(evidence_mails) > 1:
            return None, {'field': 'evidence', 'code': 'SUPPLIER_EVIDENCE_MIXED',
                          'mail_ids': sorted(evidence_mails)}, None
        if supplier_name is None:
            return None, {'field': 'supplier_name', 'code': 'MISSING_FIELD'}, None
        if named is None:
            return None, {'field': 'supplier_name', 'code': 'SUPPLIER_NOT_MATCHED'}, None
        return named, None, matched_name
    if len(linked) > 1:
        return None, {'field': 'evidence', 'code': 'SUPPLIER_EVIDENCE_MIXED', 'mail_ids': sorted(evidence_mails)}, None
    supplier_id = linked.pop()
    if named is not None and named != supplier_id:
        return None, {'field': 'supplier_name', 'code': 'SUPPLIER_MISMATCH'}, None
    return supplier_id, None, matched_name if named is not None else None


# 거래처는 법인 표기 없이 등록되어 있다. 업체명 앞뒤에 붙은 법인 표기만 뺀다('동해프레시 주식회사', '(주)청솔농산')
_LEGAL_FORM = re.compile(r'주식회사|유한책임회사|유한회사|합자회사|합명회사|농업회사법인|영농조합법인|'
                         r'\(\s*(?:주|유)\s*\)|㈜|㈲')
# 법인 표기 외에 등록 이름과 달라지게 하는 표기: 부서·직책. 처음 나온 위치부터 뒤는 회사명이 아니다
_NAME_DECORATION = re.compile(r'영업[부팀]|구매[부팀]|대표이사|과장|대리|팀장|부장|사원|드림')


def _company_name(text: str | None) -> str:
    """법인 표기를 빼고 띄어쓰기를 한 칸으로 줄인 업체명 ('동해프레시 주식회사' → '동해프레시')."""
    return re.sub(r'\s+', ' ', _LEGAL_FORM.sub(' ', str(text or ''))).strip()


def _resolve_supplier_name(store: QuoteStore, case_id: str, supplier_name: str | None) -> tuple[str | None, str | None]:
    """업체명으로 등록 업체 하나를 찾는다. 반환: (supplier_id, 찾은 이름).
    적힌 그대로 정확히 같은 업체를 먼저 찾고, 없으면 법인 표기를 뺀 이름, 그다음 부서·직책 표기 앞부분만 남긴 이름
    ('한결냉동식품 영업팀 최서윤' → '한결냉동식품')으로 차례로 정확히 찾는다.
    이름이 서로 포함되는 정도로는 찾지 않는다(저장소 조회는 정확 일치이고, 여럿이면 찾지 않는다).
    찾은 이름은 등록 이름과 같으므로, 저장할 업체명 값으로 쓰면 DB의 업체 근거 검사(등록 이름과 같음)를 통과한다."""
    if not supplier_name or not supplier_name.strip():
        return None, None
    named = _read(lambda: store.resolve_supplier(case_id, supplier_name))
    if named is not None:
        return named, supplier_name
    company = _company_name(supplier_name)
    decoration = _NAME_DECORATION.search(company)
    tried = {supplier_name}
    for name in (company, company[:decoration.start()].strip() if decoration else ''):
        if name and name not in tried:
            tried.add(name)
            named = _read(lambda: store.resolve_supplier(case_id, name))
            if named is not None:
                return named, name
    return None, None


# SUPPLIER_NOT_MATCHED 뒤에 할 일. 법인 표기·부서·직책은 도구가 이미 빼고 찾았으므로 다시 보내지 않는다
_SUPPLIER_NOT_MATCHED_NEXT = ('등록된 거래처에 없다. 다시 보내지 말고(업체명을 빼고 보내지도 않는다) '
                              '바로 HOLD로 최종 응답한다. hold.code는 SUPPLIER_NOT_MATCHED다.')
# NO_QUOTE_DATA(이전 견적 없는 업체의 단가 없는 메일) 뒤에 할 일
_NO_QUOTE_DATA_NEXT = '이 업체의 이전 견적이 없고 단가도 없다. 다시 보내지 말고 바로 HOLD로 최종 응답한다. hold.code는 NO_QUOTE_DATA다.'


def _carry(previous: tuple[str, QuoteVersionRecord], values: dict, tiers: list, costs: list,
           review: list) -> tuple[list, list, list]:
    """이번 메일에 없는 값을 이전 버전에서 원래 근거와 함께 이어받는다. 반환: (tiers, costs, 이어받은 항목).
    단위·통화·발주 단위 등 단가의 뜻이 이전과 달라졌으면 이전 단가·단가 구간·그 조건은 이어받지 않는다."""
    previous_id, previous_record = previous
    before = previous_record.evidence_jsonb
    basis_changed = any(name in values and name in before and values[name]['value'] != before[name]['value']
                        for name in PRICE_BASIS)
    price_given = 'unit_price' in values or bool(tiers)
    # 이번 메일이 단가 구간을 줬으면 이전 단일 단가는 이어받지 않는다
    skip = {'unit_price', *PRICE_BASIS} if basis_changed else ({'unit_price'} if tiers else set())
    carried = []
    for name, entry in before.items():
        if name in values or name in skip or QUOTE_FIELDS.get(name, ('', 'mail'))[1] in ('mail', 'supplier'):
            continue
        values[name] = {**entry, 'carried_from': entry.get('carried_from', previous_id)}
        carried.append(name)
    # 이번 메일이 단일 단가를 줬으면 이전 단가 구간도 이어받지 않는다 (새 단가가 구간을 대신한다)
    if not price_given and previous_record.pricing_terms_jsonb.get('price_tiers') and not basis_changed:
        tiers = previous_record.pricing_terms_jsonb['price_tiers']
        carried.append('price_tiers')
    # 비용은 종류 단위로 합친다. 이번 메일이 말한 종류(포함·무료·없음 포함)는 이번 것만 쓰고, 말하지 않은 종류는 이어받는다
    mentioned = {cost['cost_type'] for cost in costs}
    kept = [cost for cost in previous_record.cost_terms_jsonb or [] if cost['cost_type'] not in mentioned]
    if kept:
        costs = costs + [{**cost, 'carried_from': cost.get('carried_from', previous_id)} for cost in kept]
        carried.append('cost_terms')
    # OTHER는 서로 다른 비용(보관비·포장비 등)이 섞이므로, 이번 메일에 없는 이전 OTHER가 빠지면 담당자 확인으로 남긴다
    def same(c): return c.get('amount'), c.get('basis'), c.get('included_in_price')   # 이전 버전 행은 키가 빠졌을 수 있다
    new_other = {same(c) for c in costs if c['cost_type'] == 'OTHER'}
    if 'OTHER' in mentioned and any(same(c) not in new_other
                                    for c in previous_record.cost_terms_jsonb or [] if c['cost_type'] == 'OTHER'):
        review.append({'field': 'cost_terms', 'code': 'OTHER_COST_DROPPED'})
    had_price = 'unit_price' in before or bool(previous_record.pricing_terms_jsonb.get('price_tiers'))
    if basis_changed and not price_given and had_price:
        review.append({'field': 'unit_price', 'code': 'PRICE_BASIS_CHANGED'})
    if 'unit_price' in values and 'unit_price' not in carried and 'unit' in carried \
            and values['unit']['mail_id'] != values['unit_price']['mail_id']:
        review.append({'field': 'unit', 'code': 'UNIT_CARRIED'})     # 새 단가의 단위는 이번 메일에서 확인되지 않았다
    return tiers, costs, carried


def _letters(text) -> str:
    """품목·규격 대조용 표기: 한글·영문·숫자만 남긴다 ('특품, 10kg/박스' → '특품10kg박스')."""
    return re.sub(r'[^0-9a-zA-Z가-힣]', '', _nfkc(str(text or ''))).casefold()


_ISSUE_EXCERPT_LIMIT = 300


def _record_supplier_issue(store: QuoteStore, context: RunContext, check: _SourceCheck, issue: dict,
                           supplier: dict | None, ref: EvidenceRef | None) -> None:
    """업체 거절을 업체명이 적힌 원문 줄과 함께 서버에 남긴다(서버가 원문과 다시 대조해 보류 사유로 쓴다).
    기록이 실패해도 도구 결과(거절과 다음 행동 안내)는 그대로 돌려준다. 실패를 올리면 ① 실행 전체가 실패로 끝나고,
    기록이 없을 때 서버는 보류 사유만 '확인 불가'로 보인다."""
    record = getattr(store, 'record_extraction_issue', None)
    if not callable(record) or supplier is None or ref is None:
        return
    lines = check.lines(ref, supplier['raw_value'])
    excerpt = (lines[0].strip() if lines else supplier['raw_value'])[:_ISSUE_EXCERPT_LIMIT]
    where = 'body' if ref.part == 'body' else f'part:{ref.part}'
    payload = {'code': issue['code'], 'field': 'supplier_name', 'supplier_name': supplier['value'],
               'evidence': [{'evidence_id': f'{ref.mail_id}:{where}', 'excerpt': excerpt}]}
    try:
        record(context.case_id, context.agent_run_id, payload)
    except Exception:
        pass


_NAME_SPACES = re.compile(r'[\s\u00a0\u3000\u200b\ufeff]+')   # DB item_name_norm과 같은 공백 종류


def _item_key(text) -> str:
    """등록 품목 대조용 품목명: 공백을 모두 빼고 소문자로 ('냉동 망고' → '냉동망고').
    DB internal.item_name_norm과 같은 기준이다. 업무 키가 없을 때(최초 추출)만 쓴다."""
    return _NAME_SPACES.sub('', str(text or '')).lower()


def _registered_item(items: list[dict], current: dict, case: dict) -> dict | None:
    """이번 메일의 품목과 같은 그 업체의 등록 품목. 하나로 정해질 때만 돌려준다(애매하면 연결하지 않는다).
    업무에 품목 키가 있으면 같은 키인 등록 품목만 본다. DB가 같은 품목을 키로만 판정하므로 이름은 비교하지 않는다.
    업무 키가 없으면(최초 추출) _item_key 기준으로 품목명이 같을 때만 본다.
    이름이 서로 포함되는 정도('냉동딸기'와 '냉동딸기 (IQF)')로는 연결하지 않는다.
    후보가 여럿이면 규격으로 좁힌다."""
    key = case.get('standardized_item_key')
    if key:
        found = [item for item in items if item.get('standardized_item_key') == key]
    else:
        name = _item_key(current.get('item_name', {}).get('value'))
        if not name:
            return None
        found = [item for item in items if _item_key(item.get('item_name')) == name]
    if len(found) > 1:
        wanted = _letters(current.get('item_name', {}).get('value')) + _letters(current.get('specification', {}).get('value'))
        found = [item for item in found if _letters(item.get('specification')) and
                 _letters(item.get('specification')) in wanted] or found
    return found[0] if len(found) == 1 else None


def _item_name_variant(name: str, case_item: str | None, item: dict | None) -> bool:
    """견적의 품목명이 업무 품목명과도, 그 업체의 등록 품목명과도 다른지(담당자 확인 대상).
    업무 품목명이 아직 없으면 확인하지 않는다. 띄어쓰기·호환 문자 차이는 같은 표기로 본다."""
    if not case_item:
        return False
    return _compact(name) not in {_compact(case_item), _compact((item or {}).get('item_name') or case_item)}


def _case_conditions(case: dict, current: dict, item: dict | None, initial_mails: set) -> dict | None:
    """이번 메일 값으로 채울 업무 조건. 업무에 아직 없는 것만 채운다.
    품목·규격은 처음 추출할 때(등록 품목이 확인되면 그 품목 키도) 채운다.
    업무 비교 수량은 기존 업체 인상 통보(INITIAL 메일)의 공급 물량(available_quantity)이다.
    단위는 그 수량의 단위, 없으면 같은 메일의 단가 단위다. 대안 업체 회신의 공급 가능량으로는 채우지 않는다."""
    conditions = {}
    if not case.get('item_name') and 'item_name' in current:
        conditions |= {'item_name': current['item_name']['value'],
                       'specification': current['specification']['value'] if 'specification' in current else None}
        if item and item.get('standardized_item_key'):
            conditions['standardized_item_key'] = item['standardized_item_key']
    supply = current.get('available_quantity')
    if supply and supply['mail_id'] in initial_mails and not case.get('quantity'):
        unit = supply.get('quantity_unit') or current.get('unit', {}).get('value')
        if unit:
            conditions |= {'quantity': supply['value'], 'unit': unit}
    return conditions or None


def _supply_quantity_missing(case: dict, source_mail: dict, values: dict) -> bool:
    """기존 업체 인상 통보(INITIAL 메일)인데 업무 비교 수량을 정할 공급 물량·단위가 없는지.
    업무 수량이 비어 있을 때만 본다. 저장한 뒤에는 보류할 수 없으므로(DB가 거부) 저장 전에 거절한다.
    단위는 공급 물량의 단위, 없으면 같은 메일의 단가 단위로 정한다(_case_conditions와 같은 기준).
    공급 물량 값이 규격·주문 단위 표기 안의 수량이면(예: '특품 10kg/박스'의 10kg) 공급 물량이 아니다 (S3-15).
    줄이 아니라 값으로 본다: 규격과 공급 계획이 한 줄에 있어도('10kg/박스, 공급 계획 1,000kg') 1,000kg는 통과한다."""
    if case.get('quantity') or source_mail.get('message_role') != 'INITIAL':
        return False
    supply = values.get('available_quantity')
    if not supply or not (supply.get('quantity_unit') or values.get('unit', {}).get('value')):
        return True
    specs = [values[name]['raw_value'] for name in ('specification', 'quantity_per_order_unit') if name in values]
    specs += [case['specification']] if case.get('specification') else []
    raw = _normalized(supply['raw_value'], True)
    return any(_contains(_normalized(spec, True), raw, True) for spec in specs)


def save_structured_quote(store: QuoteStore, context: RunContext, fields: list[QuoteField],
                          price_tiers: list[PriceTier], cost_terms: list[CostTerm], item_notes: list[ItemNote]) -> dict:
    """store_structured_mail의 본체. 업무·revision은 실행 정보(context)에서만 받는다."""
    case_id = context.case_id
    case = _read(lambda: store.get_work_case(case_id))
    if case is None:
        return {'status': 'CASE_NOT_FOUND'}
    if case.get('input_revision') != context.input_revision:
        return {'status': 'REVISION_CHANGED'}

    check, rejected, review, adjusted = _SourceCheck(store, case_id), [], [], []
    values, sent_null, seen, refs, scale_texts = {}, [], set(), {}, {}
    for field in fields:
        if field.field_name not in QUOTE_FIELDS:
            rejected.append({'field': field.field_name, 'code': 'UNKNOWN_FIELD'})
            continue
        if field.field_name in seen:
            rejected.append({'field': field.field_name, 'code': 'DUPLICATE_FIELD'})
            continue
        if field.tax_basis and field.field_name != 'tax_terms':
            rejected.append({'field': field.field_name, 'code': 'TAX_BASIS_NOT_TAX_TERMS'})
            continue
        if field.unit_code and field.field_name not in _UNIT_FIELDS:
            rejected.append({'field': field.field_name, 'code': 'UNIT_CODE_NOT_UNIT_FIELD'})
            continue
        if field.price_scale_text and QUOTE_FIELDS[field.field_name][0] != 'price':
            rejected.append({'field': field.field_name, 'code': 'PRICE_SCALE_INVALID'})
            continue
        scale_texts[field.field_name] = field.price_scale_text
        seen.add(field.field_name)
        if field.raw_value is None or not field.raw_value.strip():
            sent_null.append(field.field_name)
            continue
        kind = QUOTE_FIELDS[field.field_name][0]
        issue, source = check(field.evidence, field.raw_value, numeric=kind in ('number', 'price'))
        if issue is None:
            try:
                value = _parse(kind, field.field_name, field.raw_value, field.unit_code)
            except ReviewRequired as error:
                issue = error.issue['code']
        if issue is None and len(value) > TEXT_FIELD_LIMITS.get(field.field_name, len(value)):
            issue = 'TOO_LONG'
        if issue:
            rejected.append({'field': field.field_name, 'code': issue})
            continue
        entry = values[field.field_name] = {'raw_value': field.raw_value, 'value': value, **source}
        refs[field.field_name] = field.evidence
        if field.field_name in _QUANTITY_FIELDS and (
                unit := _quantity(field.raw_value, field.field_name, field.unit_code)[1]):
            entry['quantity_unit'] = unit
        if kind == 'price' and (currency := _price(field.raw_value, field.field_name)[1]):
            entry['currency'] = currency
        if field.tax_basis:             # 근거 원문과 함께 저장되고, 다음 버전으로도 이어받는다
            entry['tax_basis'] = field.tax_basis
    # 단가의 배수·통화·단위 짝을 원문 줄과 대조한다. 숫자만 맞고 뜻이 다른 단가가 CONFIRMED로 저장되지 않게
    foreign_given = values.get('currency', {}).get('value') not in (None, 'KRW')
    for name in ('unit_price', 'previous_unit_price'):
        if name not in values:
            continue
        scale, problem = _price_issue(check, refs[name], values[name]['raw_value'], scale_texts.get(name), foreign_given)
        if problem is None and name == 'unit_price' and 'unit' in values:
            problem = _unit_pair_issue(check, refs[name], values[name]['raw_value'], refs['unit'], values['unit'])
        if problem:
            rejected.append({'field': name, **problem})
        elif scale != 1:
            values[name] |= {'value': _scaled(values[name]['value'], scale), 'price_scale_text': scale_texts[name]}
    tiers = _tiers(check, price_tiers, rejected, adjusted, foreign_given, values.get('unit'))
    costs = _costs(check, cost_terms, rejected, review)
    notes = []
    for index, note in enumerate(item_notes):
        issue, source = check(note.evidence, note.text)
        if issue is None and len(note.text) > NOTE_TEXT_LIMIT:
            issue = 'TOO_LONG'
        if issue:
            rejected.append({'field': f'item_notes[{index}]', 'code': issue})
        elif note.evidence.mail_id in check.instruction_mails and _INSTRUCTION.search(note.text):
            # 보안 예외: 지시문이 있는 메일에서 지시문 문장 자체는 비고로 저장하지 않는다(③④⑤에 전달되지 않게)
            adjusted.append({'field': f'item_notes[{index}]', 'code': 'INSTRUCTION_NOT_NOTE'})
        else:
            notes.append({'text': note.text, 'category': note.category, **source})

    # 단가·구간 단가에 외화가 적혀 있으면 그 통화가 견적 통화다 ('3.20 USD'). 단가끼리 통화가 다르면 거절한다
    marked = [(entry, entry['currency']) for entry in [values.get('unit_price', {}), *tiers] if entry.get('currency')]
    price_currency = marked[0][1] if marked else None
    if len({currency for _, currency in marked}) > 1 or \
            (price_currency and 'currency' in values and values['currency']['value'] != price_currency):
        rejected.append({'field': 'currency', 'code': 'CURRENCY_MISMATCH'})
    elif price_currency and 'currency' not in values:
        source = marked[0][0]
        values['currency'] = {'raw_value': source.get('raw_value') or source.get('unit_price_raw'),
                              'value': price_currency, 'mail_id': source['mail_id'], 'location': source['location']}
    # 구간마다 통화·단위를 남긴다. 구간 단가는 견적 통화·단위를 따르고, 다른 통화·붙은 단위는 위에서 거절했다
    for tier in tiers:
        tier |= {'currency': price_currency or values.get('currency', {}).get('value') or 'KRW',
                 'unit': values.get('unit', {}).get('value')}
    # 적용일보다 이른 유효기간은 회신 기한이다. DB 제약(valid_until >= effective_from)에 닿기 전에 거절한다
    if 'valid_until' in values and 'effective_from' in values and \
            values['valid_until']['value'] < values['effective_from']['value']:     # ISO 날짜 문자열
        rejected.append({'field': 'valid_until', 'code': 'VALID_UNTIL_BEFORE_EFFECTIVE'})
    # 이번 호출의 모든 근거는 한 업체의 메일이어야 한다
    case_mails = {mail['mail_id']: mail for mail in _read(lambda: store.list_case_mails(case_id))}
    evidence_mails = {entry['mail_id'] for entry in [*values.values(), *tiers, *costs, *notes]}
    supplier_name = values['supplier_name']['value'] if 'supplier_name' in values else None
    supplier_id, issue, matched_name = _quote_supplier(store, case_id, case_mails, evidence_mails, supplier_name)
    if matched_name and matched_name != supplier_name:
        # 법인 표기·부서·직책을 빼고 찾았으면 저장 값은 등록 이름, raw_value·근거는 원문 표기 그대로 둔다
        values['supplier_name']['value'] = matched_name
    # 원문 근거가 확인된 업체명이 등록 거래처와 맞지 않으면(미등록·연결 업체와 다름) 다른 항목의 거절보다 먼저 알린다.
    # 다른 항목을 고쳐도 저장할 수 없는 견적이라, 다시 보내기를 다른 항목에 쓰고 원인을 잃지 않게 한다
    # (실제 LLM 재현: 규격 근거 거절로 두 번 다시 보내고 SUPPLIER_MISMATCH에 닿지 못함)
    if rejected and not (supplier_name and issue and issue['code'] in ('SUPPLIER_NOT_MATCHED', 'SUPPLIER_MISMATCH')):
        return _with_hints({'status': 'REJECTED', 'rejected': rejected}, rejected)
    if issue:
        if issue['code'] in {'SUPPLIER_NOT_MATCHED', 'SUPPLIER_MISMATCH'}:
            _record_supplier_issue(store, context, check, issue, values.get('supplier_name'), refs.get('supplier_name'))
        result = _with_hints({'status': 'REJECTED', 'rejected': [issue]}, [issue])
        if issue['code'] == 'SUPPLIER_NOT_MATCHED':
            result['next'] = _SUPPLIER_NOT_MATCHED_NEXT
        return result
    # 원문 메일은 근거 메일 중 가장 최근에 받은 메일이다 (회신이 이전 메일의 값을 함께 근거로 써도 회신이 원문)
    source_mail_id = max(evidence_mails, key=lambda mail_id: (case_mails[mail_id].get('received_at') or '', mail_id))
    if _supply_quantity_missing(case, case_mails[source_mail_id], values):
        missing = {'field': 'available_quantity', 'code': 'SUPPLY_QUANTITY_MISSING'}
        return _with_hints({'status': 'REJECTED', 'rejected': [missing]}, [missing])
    if check.instruction_mails & evidence_mails:
        review.append({'field': 'evidence', 'code': 'INSTRUCTION_IN_MAIL'})    # 메일에 AI·시스템에게 하는 문장이 있음

    current = dict(values)
    previous = _read(lambda: store.latest_quote_version(case_id, supplier_id))
    # 기준 단가와 크기를 비교한다. 배수·통화·단위를 잘못 읽은 단가는 표기와 상관없이 기준과 크게 차이 난다
    if implausible := _implausible_prices(check, values, refs, tiers, price_tiers, previous):
        return _with_hints({'status': 'REJECTED', 'rejected': implausible}, implausible)
    carried = []
    if previous:
        tiers, costs, carried = _carry(previous, values, tiers, costs, review)
    elif 'unit_price' not in values and not tiers:
        # 이전 견적이 없는 업체의 단가 없는 메일(비용 조건만 등)은 견적이 아니다. LLM은 업체별 이전 견적을 구분하지 못한다
        missing = {'field': 'unit_price', 'code': 'NO_QUOTE_DATA'}
        return {**_with_hints({'status': 'REJECTED', 'rejected': [missing]}, [missing]), 'next': _NO_QUOTE_DATA_NEXT}
    unit_missing ='unit_price' in values and 'unit' not in values
    if unit_missing:
        # 단위 없는 단가는 비교할 수 없다. 견적 열에는 넣지 않고 메일 정규화 값에만 원문과 함께 남긴다
        del values['unit_price']
        review.append({'field': 'unit_price', 'code': 'UNIT_MISSING'})

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
    foreign = value('currency') not in (None, 'KRW')
    quantity_units = {f'{name}_unit': values[name]['quantity_unit'] for name in sorted(_QUANTITY_FIELDS)
                      if values.get(name, {}).get('quantity_unit')}
    item = _registered_item(_read(lambda: store.list_supplier_items(case_id, supplier_id)) or [], current, case)
    # 같은 품목인지는 LLM이 판단한다(업무 품목의 견적만 보낸다). 이름 표기가 업무와도, 그 업체의 등록 품목명과도 다르면
    # 담당자 확인만 남긴다 (우리가 보낸 메일의 등록 품목명으로 회신한 경우는 확인하지 않는다)
    if 'item_name' in values and _item_name_variant(values['item_name']['value'], case.get('item_name'), item):
        review.append({'field': 'item_name', 'code': 'ITEM_NAME_VARIANT'})
    initial_mails = {mail_id for mail_id, mail in case_mails.items() if mail.get('message_role') == 'INITIAL'}
    conditions = _case_conditions(case, current, item, initial_mails)
    record = QuoteVersionRecord(
        work_case_id=case_id, supplier_id=supplier_id, source_mail_id=source_mail_id,
        version_no=previous[1].version_no + 1 if previous else 1, supersedes_id=previous[0] if previous else None,
        unit_price=value('unit_price'), unit=value('unit'), shipping_cost=shipping,
        available_quantity=value('available_quantity'), effective_from=value('effective_from'),
        valid_until=value('valid_until'),
        checks_jsonb={name: {'state': 'STATED', 'value': v} for name, v in grouped('checks').items()} | (
            {'tax_basis': {'state': 'STATED', 'value': values['tax_terms']['tax_basis']}}
            if values.get('tax_terms', {}).get('tax_basis') else {}),
        pricing_terms_jsonb=grouped('pricing') | quantity_units | ({'price_tiers': tiers} if tiers else {}),
        cost_terms_jsonb=costs, item_notes_jsonb=notes,
        evidence_jsonb={name: entry for name, entry in values.items() if QUOTE_FIELDS[name][1] != 'mail'},
        extraction_status='NEEDS_REVIEW' if foreign or review else 'CONFIRMED',
        currency=value('currency') or 'KRW', mail_normalized=mails,
        case_conditions=conditions, supplier_item_id=item['id'] if item else None)
    if previous and _same_quote(previous[1], record):
        saved = previous                     # 같은 메일을 다시 추출해 저장 값이 같으면 새 버전을 만들지 않는다
    else:
        saved = store.insert_quote_version(record, context.input_revision)
    if saved is None:
        return {'status': 'REVISION_CHANGED'}
    quote_version_id, row = saved            # 같은 내용이 이미 있으면 기존 행이다. 응답은 실제 행 기준
    unknown = [name for name in sent_null if name not in values]
    if any(item['code'] == 'PRICE_BASIS_CHANGED' for item in review) and 'unit_price' not in unknown:
        unknown.append('unit_price')
    if unit_missing:
        unknown += [name for name in ('unit_price', 'unit') if name not in unknown]
    return _with_hints({'status': 'STORED', 'quote_version_id': quote_version_id, 'version_no': row.version_no,
                        'supersedes_id': row.supersedes_id, 'extraction_status': row.extraction_status,
                        'unknown_fields': unknown, 'carried_fields': carried, 'review': review,
                        'adjusted': adjusted}, adjusted)


# 저장 값 비교에서 빼는 항목. 근거 위치·이어받은 표시·메일 정규화 값이 달라도 견적 내용은 같다
_SAME_QUOTE_EXCLUDE = {'version_no', 'supersedes_id', 'case_conditions', 'evidence_jsonb', 'mail_normalized'}


def _same_quote(before: QuoteVersionRecord, record: QuoteVersionRecord) -> bool:
    """같은 원문 메일에서 저장 값이 모두 같은 견적이다 (보낸 항목 구성이 달라 일부를 이어받은 경우 포함)."""
    return before.source_mail_id == record.source_mail_id and \
        before.model_dump(exclude=_SAME_QUOTE_EXCLUDE) == record.model_dump(exclude=_SAME_QUOTE_EXCLUDE)


def _with_hints(result: dict, items: list[dict]) -> dict:
    """거절·조정 항목의 코드별 고치는 방법을 hints로 붙인다."""
    hints = {item['code']: _HINTS[item['code']] for item in items if item['code'] in _HINTS}
    return result | {'hints': hints} if hints else result


@register
@tool
def store_structured_mail(fields: list[QuoteField], runtime: ToolRuntime[RunContext],
                          price_tiers: list[PriceTier] | None = None, cost_terms: list[CostTerm] | None = None,
                          item_notes: list[ItemNote] | None = None) -> dict:
    '''메일에서 읽은 견적을 원문과 대조·검증한 뒤 이 업무의 새 견적 버전으로 저장한다.

    입력: 모든 값은 원문에 적힌 그대로, 근거 evidence{mail_id, part('body' 또는 attachment_ref), page, cell}와 함께 준다.
    fields: field_name과 raw_value. 원문에서 찾았지만 값이 없으면 raw_value를 null로 둔다.
      supplier_name(메일이 업체에 연결돼 있지 않으면 필수. 연결돼 있어도 원문에 회사명이 있으면 보낸다 — 연결된 업체와
        다른 등록 업체이면 SUPPLIER_MISMATCH), item_name, specification, origin, certifications, tax_terms,
      lead_time, delivery_date(납품·입고일), unit_price, unit, available_quantity, effective_from(적용일),
      valid_until(유효기간), currency, order_unit, quantity_per_order_unit, moq, pack_multiple,
      previous_unit_price(인상 전 단가), increase_reason(인상 사유)
    raw_value는 원문에서 값 부분만 그대로 옮긴다 (예: unit_price "54,500원" 또는 "3.20 USD", unit "박스",
      valid_until "2026년 12월 31일", quantity_per_order_unit "박스당 10kg").
    단가의 배수·통화는 원문 그 단가 줄 그대로 옮긴다("9,700천원", "USD 3.10"). 배수가 표 머리글에만 있으면
      ("(단위: 천원)") 단가(unit_price·previous_unit_price·price_tiers)에 price_scale_text로 그 구절을 붙인다.
      unit은 단가 바로 뒤나 같은 줄의 단위다("54,500원/박스 (박스당 10kg)" → unit "박스").
    price_tiers·cost_terms·item_notes는 fields가 아니라 각 인자로 보낸다.
    price_tiers: 단가 구간 min_quantity·max_quantity(원문 그대로, 원문에 없으면 null)·unit_price
      "이상·초과"는 min_quantity, "미만·이하"는 max_quantity다 (예: "500kg 이상 ~ 1,000kg 미만" → min "500kg 이상",
      max "1,000kg 미만"). 한 칸에 둘 다 넣으면 TIER_BOUND_MISPLACED로 거절한다.
    cost_terms: 비용 cost_type(SHIPPING/HANDLING/OTHER)·source_text(그 비용이 적힌 원문 구절)·amount·basis·payer·
      included_in_price(비용마다 꼭 보낸다: 단가에 포함 true, 별도 청구 false, 원문에 없으면 null).
      "없음"·"무료"는 amount에 그대로 적는다.
    item_notes: 견적 판단에 영향을 줄 수 있는 조건 원문과 분류 category(RECEIVING 입고·QUALITY 품질·PACKING 포장·
      PAYMENT 결제·SUPPLY 공급·PRICE_NOTICE 가격 예고). 어느 분류에도 맞지 않는 인사·안내 문장과 필드로 보낸
      날짜·단가 문장은 비고가 아니다.
    한 번의 호출에는 한 업체의 메일 근거만 쓴다. 업체는 메일의 업체 연결로 정해진다.
    같은 업체의 이전 견적에 있던 값은 도구가 이어받으므로 이번 메일에 없는 값을 다시 적지 않는다.
    단위·통화가 이전 견적과 달라졌으면 이전 단가는 이어받지 않는다.

    반환 status:
    - STORED: quote_version_id·version_no·extraction_status·unknown_fields(값이 없는 필드)·
      review(field·code: 확인이 필요해 NEEDS_REVIEW로 저장한 항목)·adjusted(field·code: 도구가 바꾸거나 뺀 항목)·hints
    - REJECTED: rejected(field·code)와 hints(code별 고치는 방법). 아무것도 저장하지 않았다.
      단가 배수·통화·단위 거절은 sent(보낸 값)·found(문제를 찾은 원문 줄 mail_id·part·page·line·text)가 붙는다.
    - REVISION_CHANGED / CASE_NOT_FOUND: 저장하지 않았다.
    '''
    context = _run_context(_quote_store, runtime)
    return save_structured_quote(_quote_store, context, fields, price_tiers or [], cost_terms or [], item_notes or [])


# ② CANDIDATE: search_supplier_evidence, create_rfq_draft
#   06 work_cases·suppliers·supplier_items·purchase_history 기준. 후보·제외·체크리스트·선호 점수는 코드가 판정하고
#   LLM은 검색어, 같은 품목 고르기, 업체별 선택 질문, 등록 비고에 근거한 주의 표시를 정한다.
#   과거 단가는 순위에도 쓰지 않는다(BR-03).

MAX_CANDIDATES = 10                  # 후보·RFQ 상한 (Notion ② 10곳)
# 선호 가중치 기본값. 회사 정책(policy_jsonb)에 값이 없을 때만 쓴다(_preferences). 후보 정렬에만 쓰고 DB에 저장하지 않는다
PREFERRED_ORIGIN, ORIGIN_WEIGHT, HISTORY_WEIGHT = '국내산', Decimal(2), Decimal(1)

# RFQ 질문. 고정 질문은 모든 업체에 묻고, 선택 질문은 LLM이 업체마다 고른다(고르지 않으면 RFQ_DEFAULT_OPTIONS).
# 업체별로 미확인 필수 조건 질문이 더해진다
RFQ_DUE_QUESTION = '요청 납기일 납품 가능 여부'
RFQ_NO_DUE_QUESTION = '납품 가능 일정(발주 후 납품까지 걸리는 기간)'    # 업무 납기일이 없을 때 대신 묻는다
RFQ_FIXED_QUESTIONS = ('단가와 단가 기준 단위', '공급 가능 수량과 그 단위', RFQ_DUE_QUESTION,
                       '운송비 부담 주체와 단가 포함 여부')
RFQ_OPTIONAL_QUESTIONS = {'MOQ': '최소 주문 수량(MOQ)', 'VALIDITY': '견적 유효기간', 'TAX': '부가세 포함 여부',
                          'PACKING': '포장 단위와 박스당 중량', 'ORIGIN': '원산지',
                          'DELIVERY_DAYS': '배송 가능 요일과 지역'}
RFQ_DEFAULT_OPTIONS = ('MOQ', 'VALIDITY')


class CandidateStore(CaseStore, Protocol):
    """search_supplier_items·search_supplier_evidence·create_rfq_draft의 후보 조회 저장소 (06).
    소유 범위는 구현이 검사한다."""

    def get_candidate_conditions(self, case_id: str) -> dict | None:
        """work_cases의 {input_revision, item_name, specification,
        mandatory_conditions(mandatory_conditions_jsonb: 인증 이름 목록 또는 [{condition}]), quantity, unit, due_date,
        policy(policy_jsonb)}. 업무가 없거나 범위 밖이면 None."""

    def incumbent_supplier_ids(self, case_id: str) -> set[str]:
        """이 업무의 quote_versions.supplier_id, 즉 인상을 요청한 업체."""

    def search_supplier_items(self, case_id: str, keywords: list[str], limit: int) -> list[dict]:
        """item_name에 keywords 중 하나라도 들어간 이 사용자의 supplier_items 행, 최대 limit개
        (06 컬럼 그대로: id, supplier_id, item_name, specification, unit, certifications_jsonb(모르면 None), origin,
        notes, last_purchase_id)."""

    def get_supplier_items(self, case_id: str, ids: list[str]) -> list[dict]:
        """id가 ids에 있는 이 사용자의 supplier_items 행 (컬럼은 search_supplier_items와 같다). 범위 밖 id는 빠진다."""

    def get_supplier(self, case_id: str, supplier_id: str) -> dict | None:
        """suppliers 행 (06 컬럼 그대로: id, name, contact_email, evidence_jsonb, source_type)."""

    def list_purchase_history(self, case_id: str, supplier_item_ids: list[str]) -> list[dict]:
        """purchase_history 행 (06 컬럼 그대로: supplier_item_id, supplier_id, transaction_date, quantity, unit,
        unit_price, total_amount, currency)."""


_candidate_store: CandidateStore | None = None


def use_candidate_store(store: CandidateStore | None) -> None:
    global _candidate_store
    _candidate_store = store


# suppliers.source_type: 웹에서 수집해 등록한 업체 (등록 거래처는 REGISTERED)
WEB_SOURCE_TYPE = 'VERIFIED_WEB'


def _key(text) -> str:
    return re.sub(r'\s+', '', str(text)).casefold()


# 인증의 우리말 공식 이름 → 영문 약칭 (대조 키). 같은 인증으로 본다
_CERT_ALIASES = {'식품안전관리인증기준': 'haccp', '식품안전관리인증': 'haccp', '안전관리인증기준': 'haccp', '해썹': 'haccp'}
# 인증 이름 뒤에 붙는 말 ('식품 안전 관리 인증 보유', 'HACCP 인증서', 'ISO 22000 인증 획득'). 떼고 다시 대조한다
_CERT_TAIL = re.compile(r'(보유|획득|취득|완료|있음|서|인증)$')


def _cert_alias(key: str) -> str:
    """공백을 뺀 인증 표기 하나의 대조 키. 공식 이름이면 약칭으로, 뒤에 붙은 말(보유·인증서 등)은 떼어 본다.
    떼어 낼 말이 없거나 다 떼면 빈 글자가 되는 표기는 그대로 둔다."""
    while key not in _CERT_ALIASES and (shorter := _CERT_TAIL.sub('', key)) and shorter != key:
        key = shorter
    return _CERT_ALIASES.get(key, key)


def _cert_key(name) -> str:
    """필수 인증 이름 하나의 대조 키: 공백·대소문자를 무시하고, 우리말 공식 이름은 약칭으로."""
    return _cert_alias(_key(name))


# 인증이 없거나 아직 아니라는 표현. 이 말이 든 항목('HACCP 미보유', 'HACCP 인증 심사 중')은 보유 인증으로 세지 않는다
_CERT_NEGATION = re.compile(r'미\s*보유|미\s*취득|미\s*인증|미\s*해당|없음|없습니다|없다|불가|만료|취소|정지|예정|'
                            r'(?:진행|심사|신청|준비)\s*중|\b(?:no|not|none|pending|expired|applying)\b', re.IGNORECASE)


def _cert_keys(names) -> set[str]:
    """보유 인증 표기들의 대조 키. 쉼표·괄호 등으로 나눈 덩어리 전체와 그 안의 띄어 쓴 낱말을 모두 키로 둔다
    ('식품안전관리인증(HACCP), ISO 22000' → {'haccp', 'iso22000', 'iso', '22000'}).
    쉼표·빗금으로 나눈 항목에 부정·미완료 표현이 있으면 그 항목은 통째로 세지 않는다
    ('ISO 22000, HACCP 미보유' → {'iso22000', 'iso', '22000'}, 'HACCP(인증 심사 중)' → 없음)."""
    keys = set()
    for name in names:
        for item in re.split(r'[,/·]+', str(name)):
            if _CERT_NEGATION.search(item):
                continue
            for chunk in re.split(r'[()（）\[\]]+', item):
                if chunk.strip():
                    keys |= {_key(chunk), *(_key(word) for word in chunk.split())}
    return {_cert_alias(key) for key in keys}


def _required_names(conditions) -> list[str]:
    """mandatory_conditions_jsonb의 인증 이름. ["HACCP"], [{"condition": "HACCP"}], {"certifications": ["HACCP"]}."""
    if isinstance(conditions, dict):
        conditions = conditions.get('certifications') or []
    names = []
    for item in conditions or []:
        name = item.get('condition') if isinstance(item, dict) else item
        if name and str(name).strip():
            names.append(str(name).strip())
    return list(dict.fromkeys(names))


def _spec_state(wanted: str, offered: str | None) -> str:
    """등록 규격이 요청 규격과 똑같으면 PASS, 그 밖은 RFQ로 확인한다(UNKNOWN). 표기만 다른 같은 규격을
    잘못 제외하지 않도록 규격 차이로는 FAIL을 내지 않는다. 쉼표·빗금 등 문장부호와 공백은 비교에서 뺀다
    ('IQF, 10kg 박스'와 'IQF 10kg/박스'는 같은 규격)."""
    if offered and _letters(wanted) == _letters(offered):
        return 'PASS'
    return 'UNKNOWN'


def _check(condition: str, required: bool, state: str, refs: list[str]) -> dict:
    return {'condition': condition, 'required': required, 'state': state, 'evidence_refs': refs}


def _required_checks(case: dict, item: dict, web: bool = False) -> list[dict]:
    """품목 한 행의 필수 조건 체크(규격·필수 인증). web이면 웹 수집 업체의 품목이다.
    웹 수집 인증은 공개 자료의 표시라 확인 전이고, 인증 정보 없이 저장된 품목도 []로 읽힌다(컬럼 기본값).
    그래서 웹 품목의 필수 인증은 PASS·FAIL로 정하지 않고 UNKNOWN으로 두어 RFQ로 묻는다."""
    ref = [f"supplier_items:{item['id']}"]
    checks = []
    if case.get('specification'):
        checks.append(_check(f"규격 {case['specification']}", True, _spec_state(case['specification'],
                                                                               item.get('specification')), ref))
    certifications = item.get('certifications_jsonb')
    unconfirmed = web or certifications is None
    owned = set() if unconfirmed else _cert_keys(certifications)
    for name in _required_names(case.get('mandatory_conditions')):
        state = 'UNKNOWN' if unconfirmed else 'PASS' if _cert_key(name) in owned else 'FAIL'
        checks.append(_check(name, True, state, ref))
    # 수량·납기는 업체 쪽에 비교할 값이 없어 판정하지 않는다. RFQ 고정 질문으로 묻는다
    return checks


def _preferences(rows: dict[str, dict], purchases: dict[str, dict],
                 policy: dict | None = None) -> dict[str, tuple[list[dict], Decimal]]:
    """업체별 (선호 체크, 점수). 선호 원산지와 같으면 원산지 가중치, 거래 이력이 있으면 이력 가중치를 더한다.
    값은 회사 정책(policy_jsonb)에서 읽고, 없거나 숫자가 아니면 PREFERRED_ORIGIN·ORIGIN_WEIGHT·HISTORY_WEIGHT다."""
    policy = policy or {}
    preferred = str(policy.get('preferred_origin') or '').strip() or PREFERRED_ORIGIN
    def weight(key, default):   # NaN·음수는 정렬을 깨거나 선호를 벌점으로 뒤집으므로 기본값을 쓴다
        value = _amount(policy.get(key))
        return value if value is not None and value.is_finite() and value >= 0 else default
    origin_weight, history_weight = weight('origin_weight', ORIGIN_WEIGHT), weight('history_weight', HISTORY_WEIGHT)
    result = {}
    for supplier_id, item in rows.items():
        origin = item.get('origin')
        origin_state = 'UNKNOWN' if not origin else 'PASS' if _key(origin) == _key(preferred) else 'FAIL'
        history_state = 'PASS' if supplier_id in purchases else 'FAIL'
        checks = [_check(f'원산지 {preferred}', False, origin_state, [f"supplier_items:{item['id']}"]),
                  _check('기존 거래 이력', False, history_state,
                         [f'purchase_history:{supplier_id}'] if history_state == 'PASS' else [])]
        score = (origin_weight if origin_state == 'PASS' else 0) + (history_weight if history_state == 'PASS' else 0)
        result[supplier_id] = (checks, Decimal(score))
    return result


def _questions(checks: list[dict], options: list[str] | None = None, has_due_date: bool = True) -> list[str]:
    """업체 한 곳의 RFQ 질문 목록. 고정 질문, 고른 선택 질문(없으면 기본 선택), 미확인 필수 조건 질문 순서다.
    업무 납기일이 없으면 본문에 없는 '요청 납기일'을 묻지 않고 납품 가능 일정을 묻는다."""
    chosen = RFQ_DEFAULT_OPTIONS if options is None else options
    extra = [f"{check['condition']} 충족 여부(증빙)" for check in checks if check['required'] and check['state'] == 'UNKNOWN']
    fixed = [q if has_due_date or q != RFQ_DUE_QUESTION else RFQ_NO_DUE_QUESTION for q in RFQ_FIXED_QUESTIONS]
    return fixed + [RFQ_OPTIONAL_QUESTIONS[key] for key in RFQ_OPTIONAL_QUESTIONS
                                        if key in chosen] + extra


def _origin_question(options: list[str] | None, origin: str | None) -> list[str]:
    """업체 한 곳의 선택 질문에서 원산지 질문을 등록 원산지로 정한다. 원산지가 있으면 빼고, 비어 있으면 더한다.
    고른 질문이 없으면(None) 기본 선택 질문에서 시작한다."""
    chosen = [key for key in (RFQ_DEFAULT_OPTIONS if options is None else options) if key != 'ORIGIN']
    return chosen if (origin or '').strip() else [*chosen, 'ORIGIN']


def _score_text(score: Decimal) -> str:
    text = format(score.quantize(Decimal('0.01')).normalize(), 'f')
    return '0' if text in ('-0', '') else text


EXCLUDE_LABELS = {'INCUMBENT': '인상을 요청한 업체', 'REQUIRED_FAIL': '필수 조건 불충족', 'OVER_LIMIT': '후보 상한 초과'}


DRAFT_SKIP_LABELS = {'NO_CONTACT': '연락처가 없는 업체'}


def search_summary(result: dict, item_name: str, drafted: list[str] | None = None,
                   not_drafted: list[dict] | None = None) -> str:
    """구매담당자가 읽는 조회 결과 한두 문장. 코드·필드 이름을 쓰지 않는다.
    drafted가 있으면(초안 생성 후) 실제로 초안을 만든 업체와 만들지 못한 업체로 쓴다."""
    scope = result['scope']
    if scope['searched'] == 0:
        # "등록 … 업체 … 없" 표현은 서버가 '거래처 미연결'로 잘못 읽는다(hold.py). 후보가 없다는 뜻만 쓴다
        return f'{item_name} 취급 업체를 찾지 못해 견적을 요청할 후보가 없습니다.'
    parts = []
    for code, label in EXCLUDE_LABELS.items():
        rows = [e for e in result['excluded'] if e['code'] == code]
        if rows:
            conditions = list(dict.fromkeys(c for e in rows for c in e.get('conditions', [])))
            parts.append(f'{label} {len(rows)}곳' + (f'({", ".join(conditions)})' if conditions else ''))
    excluded = f' {", ".join(parts)}을 제외했습니다.' if parts else ''
    if not result['candidates']:
        return f'{item_name} 취급 등록 업체 {scope["searched"]}곳을 확인했습니다.{excluded} 견적을 요청할 후보가 없습니다.'
    name_of = {c['supplier_id']: c.get('supplier_name') or c['supplier_id'] for c in result['candidates']}
    targets = drafted if drafted is not None else list(name_of)
    text = (f'{item_name} 취급 등록 업체 {scope["searched"]}곳을 확인했습니다.{excluded} '
            f'{len(targets)}곳({", ".join(name_of.get(sid, sid) for sid in targets)})에 견적을 요청합니다.')
    for code, label in DRAFT_SKIP_LABELS.items():
        skipped = [r['supplier_id'] for r in not_drafted or [] if r['code'] == code and r['supplier_id'] in name_of]
        if skipped:
            text += f' {label} {len(skipped)}곳({", ".join(name_of[sid] for sid in skipped)})은 초안을 만들지 못했습니다.'
    return text


def candidate_summary(checklist: list[dict]) -> str:
    """후보 한 곳의 사유 개조식 한 줄(명사형 종결). 필수 조건 중 확인된 것과 견적으로 확인할 것을 나눈다."""
    passed = [c['condition'] for c in checklist if c['required'] and c['state'] == 'PASS']
    unknown = [c['condition'] for c in checklist if c['required'] and c['state'] == 'UNKNOWN']
    parts = ([f'필수 조건 {"·".join(passed)} 충족'] if passed else []) + \
            ([f'{"·".join(unknown)} 견적 회신으로 확인 필요'] if unknown else [])
    return ', '.join(parts) or '필수 조건 모두 충족'


SEARCH_ITEMS_LIMIT = 30              # 품목 검색 결과 상한. 넘으면 truncated로 알리고 검색어를 좁히게 한다
MAX_KEYWORDS = 5


def find_supplier_items(store: CandidateStore, context: RunContext, keywords: list[str]) -> dict:
    """search_supplier_items의 본체. 품목명에 검색어가 들어간 등록 품목을 업체 이름과 함께 돌려준다(가격 없음)."""
    words = list(dict.fromkeys(word.strip() for word in keywords if word.strip()))[:MAX_KEYWORDS]
    if not words:
        return {'status': 'REJECTED', 'code': 'NO_KEYWORDS', 'hint': '업무 품목명에서 검색어를 하나 이상 정해 보낸다.'}
    rows = _read(lambda: store.search_supplier_items(context.case_id, words, SEARCH_ITEMS_LIMIT + 1))
    names = {sid: (_read(lambda sid=sid: store.get_supplier(context.case_id, sid)) or {}).get('name')
             for sid in dict.fromkeys(row['supplier_id'] for row in rows)}
    items = [{'supplier_item_id': row['id'], 'supplier_name': names[row['supplier_id']], 'item_name': row['item_name'],
              'specification': row.get('specification'), 'origin': row.get('origin')} for row in rows[:SEARCH_ITEMS_LIMIT]]
    return {'status': 'OK', 'keywords': words, 'items': items, 'truncated': len(rows) > SEARCH_ITEMS_LIMIT}


@register
@tool
def search_supplier_items(keywords: list[str], runtime: ToolRuntime[RunContext]) -> dict:
    '''등록 업체의 취급 품목 중 품목명에 검색어가 들어간 것을 찾는다. 업무 품목과 같은 품목을 고르기 위한 검색이다.
    keywords: 업무 품목명에서 정한 검색어 (예: "IQF 딸기 (냉동)" → ["딸기"]). 최대 5개.

    반환 status:
    - OK: items(supplier_item_id·supplier_name·item_name·specification·origin), truncated(결과가 많아 잘렸다.
      검색어를 좁혀 다시 찾는다)
    - REJECTED: code NO_KEYWORDS
    '''
    return find_supplier_items(_candidate_store, _run_context(_candidate_store, runtime), keywords)


def evaluate_candidates(store: CandidateStore, context: RunContext, supplier_item_ids: list[str]) -> dict:
    """search_supplier_evidence의 본체. create_rfq_draft도 같은 판정으로 대상 업체를 다시 확인한다.
    supplier_item_ids는 LLM이 search_supplier_items 결과에서 업무 품목과 같다고 고른 등록 품목이다.
    가: 고른 품목 없음 → 조회 범위 밖 / 나: 인상 요청 업체 → INCUMBENT / 다: 필수 FAIL → REQUIRED_FAIL /
    라: 필수 UNKNOWN → 후보(RFQ로 확인) / 마: 필수 모두 PASS → 후보"""
    case = _read(lambda: store.get_candidate_conditions(context.case_id))
    if case is None:
        return {'status': 'CASE_NOT_FOUND'}
    if case.get('input_revision') != context.input_revision:
        return {'status': 'REVISION_CHANGED'}
    item_name = case.get('item_name')
    missing = [key for key in ('item_name', 'quantity', 'unit') if not case.get(key)]
    if missing:     # 품목·수량·단위 없이 RFQ를 만들지 않는다
        labels = '·'.join({'item_name': '품목', 'quantity': '수량', 'unit': '단위'}[key] for key in missing)
        return {'status': 'CONDITIONS_MISSING', 'missing': missing,
                'summary': f'확정되지 않은 요청 조건({labels})이 있어 견적을 요청할 후보 업체를 찾을 수 없습니다.',
                'next': '다시 검색해도 결과가 같다. 다른 도구를 부르지 말고 바로 HOLD로 최종 응답한다. reason에는 summary를 쓴다.'}
    ids = list(dict.fromkeys(supplier_item_ids))
    items = _read(lambda: store.get_supplier_items(context.case_id, ids)) if ids else []
    unknown = sorted(set(ids) - {item['id'] for item in items})
    if unknown:
        return {'status': 'REJECTED', 'code': 'UNKNOWN_ITEMS', 'supplier_item_ids': unknown,
                'hint': 'search_supplier_items 결과의 supplier_item_id만 보낸다.'}
    incumbents = _read(lambda: store.incumbent_supplier_ids(context.case_id))
    suppliers = {sid: _read(lambda sid=sid: store.get_supplier(context.case_id, sid)) or {}
                 for sid in dict.fromkeys(item['supplier_id'] for item in items)}
    purchases = {}                      # 업체별 마지막 실거래 (transaction_date 기준)
    for row in sorted(_read(lambda: store.list_purchase_history(context.case_id, [item['id'] for item in items])),
                      key=lambda row: str(row.get('transaction_date') or '')):
        purchases[row['supplier_id']] = row

    web = {sid for sid, supplier in suppliers.items() if supplier.get('source_type') == WEB_SOURCE_TYPE}
    excluded, best = [], {}
    for item in items:
        supplier_id = item['supplier_id']
        if supplier_id in incumbents:
            continue
        checks = _required_checks(case, item, web=supplier_id in web)
        rank = (sum(c['state'] == 'FAIL' for c in checks), -sum(c['state'] == 'PASS' for c in checks))
        if supplier_id not in best or rank < best[supplier_id][0]:      # 업체의 품목 행 중 가장 잘 맞는 행
            best[supplier_id] = (rank, item, checks)
    for supplier_id in sorted({item['supplier_id'] for item in items} & incumbents):
        excluded.append({'supplier_id': supplier_id, 'code': 'INCUMBENT'})
    passed = {}
    for supplier_id, (_, item, checks) in sorted(best.items()):
        failed = [c['condition'] for c in checks if c['state'] == 'FAIL']
        if failed:
            excluded.append({'supplier_id': supplier_id, 'code': 'REQUIRED_FAIL', 'conditions': failed})
        else:
            passed[supplier_id] = (item, checks)

    preferences = _preferences({sid: item for sid, (item, _) in passed.items()}, purchases, case.get('policy') or {})
    candidates = []
    for supplier_id, (item, checks) in passed.items():
        preference_checks, score = preferences[supplier_id]
        checklist = checks + preference_checks
        supplier = suppliers[supplier_id]
        refs = [f"supplier_items:{item['id']}"] + (
            [f'suppliers:{supplier_id}.evidence_jsonb'] if supplier.get('evidence_jsonb') else [])
        notes = (item.get('notes') or '').strip() or None
        candidates.append({
            'supplier_id': supplier_id, 'supplier_name': supplier.get('name'),
            'has_contact': bool((supplier.get('contact_email') or '').strip()),
            'checklist': checklist, 'preference_score': _score_text(score), 'evidence_refs': refs,
            'provisional': supplier_id in web,      # 웹 수집 업체: 잠정 후보, 회신으로 확인
            'origin': item.get('origin'),
            # 후보 업체 자신의 등록 비고. LLM이 주의 표시 근거로만 쓴다. 제외·점수 근거가 아니다(BR-15)
            'notes': notes, 'notes_ref': f"supplier_items:{item['id']}.notes" if notes else None,
            '_sort': (-score, -sum(c['state'] == 'PASS' for c in checklist),
                      sum(c['state'] == 'UNKNOWN' for c in checklist), supplier_id)})
    candidates.sort(key=lambda c: c.pop('_sort'))
    over = candidates[MAX_CANDIDATES:]
    candidates = candidates[:MAX_CANDIDATES]
    excluded += [{'supplier_id': c['supplier_id'], 'code': 'OVER_LIMIT'} for c in over]
    result = {'status': 'OK',
              'scope': {'searched': len({item['supplier_id'] for item in items}),
                        'candidates': len(candidates), 'excluded': len(excluded)},
              'rfq_facts': {'품목': item_name, '규격': case.get('specification'),
                            '수량': f"{_mail_quantity(case['quantity'])}{case.get('unit') or ''}" if case.get('quantity') else None,
                            '납기일': case.get('due_date')},
              'candidates': candidates, 'excluded': excluded,
              'question_options': dict(RFQ_OPTIONAL_QUESTIONS), 'default_question_options': list(RFQ_DEFAULT_OPTIONS)}
    for entry in excluded:
        entry['label'] = EXCLUDE_LABELS[entry['code']]
    result['summary'] = search_summary(result, item_name)
    if not candidates:      # 초안을 만들 업체가 없다. LLM이 빈 목록으로 초안을 시도하지 않게 다음 행동을 알려준다
        result['next'] = '후보가 없다. create_rfq_draft를 부르지 말고 바로 HOLD로 최종 응답한다. reason에는 summary를 쓴다.'
    return result


@register
@tool
def search_supplier_evidence(supplier_item_ids: list[str], runtime: ToolRuntime[RunContext]) -> dict:
    '''업무 품목과 같다고 고른 등록 품목의 업체 중 문의(RFQ) 대상 후보를 판정한다.
    supplier_item_ids: search_supplier_items 결과에서 업무 품목과 같은 품목만 고른 supplier_item_id. 같은 품목이
      없으면 빈 목록으로 보낸다(후보 없음). 업무 조건은 도구가 읽는다.
    인상을 요청한 업체(INCUMBENT)와 필수 조건이 맞지 않는 업체(REQUIRED_FAIL)는 제외하고,
    필수 조건을 모르는 업체는 후보에 넣어 RFQ로 확인한다. 가격은 돌려주지 않는다.

    반환 status:
    - OK: scope(조회한 업체 수·후보 수·제외 수),
      candidates(supplier_id·supplier_name·checklist·preference_score·evidence_refs·origin·
      notes(후보 업체의 등록 비고)·notes_ref),
      excluded(supplier_id·code: INCUMBENT·REQUIRED_FAIL·OVER_LIMIT·label: 한글 사유),
      question_options(업체마다 고를 수 있는 선택 질문 {ID: 질문}), default_question_options(고르지 않을 때 묻는 ID),
      summary(구매담당자가 읽는 조회 결과 문장. HOLD면 reason에 이 문장을 쓴다)
    - REJECTED: code UNKNOWN_ITEMS(search_supplier_items에 없던 ID)
    - CONDITIONS_MISSING: 업무 품목·수량·단위 중 비어 있는 것이 있다(missing) / REVISION_CHANGED / CASE_NOT_FOUND
    메일에 들어갈 품목·수량·납기 값은 돌려주지 않는다. 메일 문안은 create_rfq_draft가 쓴다.
    '''
    result = evaluate_candidates(_candidate_store, _run_context(_candidate_store, runtime), supplier_item_ids)
    result.pop('rfq_facts', None)       # 메일 문안용 값. LLM이 쓸 일이 없다
    if result.get('status') == 'OK' and not result.get('candidates'):
        result['next'] = ('등록 후보가 없다. create_rfq_draft를 부르지 말고 '
                          'crawl_supplier_candidates로 웹 근거를 한 번 수집한 뒤 HOLD한다.')
    return result


# ② create_rfq_draft (초안 도구는 역할별로 나눈다: RFQ·CLARIFICATION·COUNTER_OFFER. 저장 본체 save_mail_drafts는 RFQ만 처리)
#   메일 문안은 코드 템플릿 하나로 쓴다. LLM은 보낼 업체와 업체별 선택 질문만 정하고, 서버가 업체마다 값을 채워
#   업체별 초안 행을 만든다. 06 mail_drafts에 DRAFT로 저장만 하고, 승인(ON)·자동 발송(OFF)은 백엔드가 발송 직전
#   설정으로 정한다.

RFQ_REPLY_DAYS = 2
# 날짜를 박지 않는다: 발송 본문은 승인한 초안과 같아야 해서, 늦게 승인하면 지난 기한이 나간다
REPLY_DEADLINE = f'메일을 받으신 날로부터 {RFQ_REPLY_DAYS}일 이내'
_DRAFT_HINTS = {
    'UNSUPPORTED_PURPOSE': '지금은 purpose RFQ만 만들 수 있다.',
    'NOT_A_CANDIDATE': 'search_supplier_evidence의 candidates에 있는 supplier_id에만 RFQ를 만든다.',
    'INCUMBENT': '인상을 요청한 업체에는 RFQ를 만들지 않는다.',
    'REQUIRED_FAIL': '필수 조건이 맞지 않는 업체에는 RFQ를 만들지 않는다.',
    'NO_CONTACT': '연락처가 없는 업체다. 이 업체는 건너뛴다.',
    'NO_SUPPLIERS': '보낼 후보가 없다. 다시 부르지 말고 HOLD로 최종 응답한다.',
    'UNKNOWN_QUESTION': 'questions의 question_ids는 search_supplier_evidence의 question_options에 있는 ID만 쓴다. '
                        '그 업체는 기본 선택 질문으로 저장했다. 다시 부르지 않는다.',
}
# 보내는 회사 문구(이름·소개·서명)는 mail_send_settings.style_jsonb에서 채운다. 없으면 그 부분을 비운다
RFQ_SUBJECT = '{보내는회사}{품목} 견적 요청드립니다'
RFQ_BODY = ('안녕하세요. {업체명} 담당자님\n'
            '{회사소개}\n\n'
            '아래 품목의 견적을 요청드립니다. {회신기한}에 회신 부탁드립니다.\n\n'
            '<요청 내용>\n'
            '{요청내용}\n\n'
            '<견적서 포함 사항>\n'
            '{질문목록}\n\n'
            '빠른 회신 주시면 내부 검토 후 신속히 연락드리겠습니다.\n'
            '감사합니다.\n'
            '{서명}')


def _mail_text(subject: str, body: str, values: dict) -> tuple[str, str]:
    """코드 템플릿 메일의 제목·본문. 비어 있는 회사 문구 줄은 지우고 문단 사이 빈 줄은 한 줄로 맞춘다."""
    return subject.format(**values), re.sub(r'\n{3,}', '\n\n', body.format(**values)).strip()


def _rfq_text(values: dict) -> tuple[str, str]:
    """업체 한 곳의 RFQ 제목·본문."""
    return _mail_text(RFQ_SUBJECT, RFQ_BODY, values)


def _style_values(style: dict) -> dict:
    """메일 스타일 설정(style_jsonb)의 보내는 회사 문구. 없으면 그 부분을 비운다."""
    company = str(style.get('company_name') or '').strip()
    return {'보내는회사': f'[{company}] ' if company else '', '회사소개': str(style.get('introduction') or '').strip(),
            '서명': str(style.get('signature') or '').strip()}


def mail_value(text, limit: int) -> str:
    """밖으로 나가는 메일에 넣는 업무 값. 메일에서 읽은 값이 그대로 다른 업체에 가지 않게 한 줄로 합치고,
    주소(URL) 표기를 지우고, limit자를 넘으면 자른다."""
    text = ' '.join(_URL.sub('', str(text or '')).split())
    return text if len(text) <= limit else text[:limit - 1].rstrip() + '…'


def recipient_item_name(store, case: dict, supplier_id: str | None) -> str:
    """메일 받는 업체에게 쓸 품목명. 그 업체의 등록 품목 중 업무 품목 키와 같은 것이 하나면 그 등록 이름,
    아니면 업무 품목명(메일에서 읽은 값)이다. 어느 쪽이든 mail_value로 정제한다."""
    name, key = case.get('item_name'), case.get('standardized_item_key')
    if key and supplier_id and hasattr(store, 'list_supplier_items'):
        items = _read(lambda: store.list_supplier_items(case['id'], supplier_id)) or []
        names = {item['item_name'] for item in items
                 if item.get('standardized_item_key') == key and (item.get('item_name') or '').strip()}
        if len(names) == 1:
            name = names.pop()
    return mail_value(name, MAIL_VALUE_LIMITS['품목']) or '요청 품목'


def _request_lines(facts: dict) -> str:
    """요청 내용 목록. 값이 있는 항목만 쓴다(없는 수량·납기를 '협의'로 채워 문장을 만들지 않는다).
    품목·규격은 메일에서 읽은 값일 수 있어 mail_value로 정제한다."""
    lines = []
    for label in ('품목', '규격', '수량', '납기일'):
        value = mail_value(facts.get(label), MAIL_VALUE_LIMITS.get(label, 60))
        if value:
            lines.append(f'- {label}: {value}')
    return '\n'.join(lines)


class SupplierQuestions(StrictModel):
    supplier_id: str
    question_ids: list[str]            # search_supplier_evidence의 question_options ID


class MailDraftRecord(StrictModel):
    """mail_drafts 한 행 (06 컬럼). 발송 관련 컬럼(send_mode·approved_*·sent_*)은 백엔드 발송 노드가 채운다."""
    work_case_id: str
    supplier_id: str
    purpose: Literal['RFQ', 'CLARIFICATION', 'COUNTER_OFFER']
    recipient: str
    subject: str
    body_text: str
    evidence_refs_jsonb: list[str]      # 근거 참조 (supplier_items·suppliers·agent_runs)
    style_snapshot_jsonb: dict
    replaces_id: str | None = None
    input_revision: int
    status: Literal['DRAFT'] = 'DRAFT'
    idempotency_key: str


class MailDraftStore(CandidateStore, Protocol):
    """초안 도구의 저장소 (06 mail_drafts·mail_send_settings)."""

    def current_send_settings(self, case_id: str) -> dict:
        """mail_send_settings.style_jsonb. 초안이 읽는 키는 company_name(보내는 회사 이름), introduction(회사 소개 한 줄),
        signature(서명)다. 없으면 {}."""

    def insert_mail_draft(self, record: MailDraftRecord, expected_input_revision: int) -> tuple[str, MailDraftRecord] | None:
        """mail_drafts에 저장하고 (id, 행)을 돌려준다. revision이 다르면 None.
        같은 idempotency_key가 있으면 새로 만들지 않고 기존 (id, 행)을 돌려준다."""


_draft_store: MailDraftStore | None = None


def use_draft_store(store: MailDraftStore | None) -> None:
    global _draft_store
    _draft_store = store


def _draft_rejected(code: str) -> dict:
    return {'status': 'REJECTED', 'code': code, 'hints': {code: _DRAFT_HINTS[code]}}


def save_mail_drafts(store: MailDraftStore, context: RunContext, purpose: str, supplier_item_ids: list[str],
                     supplier_ids: list[str], questions: list[SupplierQuestions] | None = None) -> dict:
    """create_rfq_draft의 본체. 코드 템플릿으로 업체별 초안 행을 만든다.
    questions는 업체별로 고른 선택 질문이다. 없는 업체는 기본 선택 질문을 쓰고, 모르는 ID가 있으면 그 업체는 기본으로 둔다.
    후보는 모두 초안 대상이므로 supplier_ids에 빠진 후보는 더한다. 원산지 질문은 등록 원산지로 정한다
    (있으면 묻지 않고, 없으면 묻는다)."""
    if purpose != 'RFQ':
        return _draft_rejected('UNSUPPORTED_PURPOSE')
    found = evaluate_candidates(store, context, supplier_item_ids)
    if found['status'] != 'OK':
        return {key: found[key] for key in ('status', 'code', 'hint', 'missing') if key in found}
    candidates = {c['supplier_id']: c for c in found['candidates']}
    excluded = {e['supplier_id']: e['code'] for e in found['excluded']}
    supplier_ids = list(dict.fromkeys([*supplier_ids, *candidates]))
    if not supplier_ids:
        return _draft_rejected('NO_SUPPLIERS')

    chosen, adjusted = {}, []
    for entry in questions or []:
        if set(entry.question_ids) <= set(RFQ_OPTIONAL_QUESTIONS):
            chosen[entry.supplier_id] = entry.question_ids
        else:
            adjusted.append({'code': 'UNKNOWN_QUESTION', 'supplier_id': entry.supplier_id})

    facts = found['rfq_facts']
    style = _read(lambda: store.current_send_settings(context.case_id)) or {}
    case = (_read(lambda: store.get_work_case(context.case_id)) or {}) | {'id': context.case_id}
    common = {'회신기한': REPLY_DEADLINE, **_style_values(style)}
    drafts, rejected = [], []
    for supplier_id in supplier_ids:
        candidate = candidates.get(supplier_id)
        if candidate is None:
            code = excluded.get(supplier_id, 'NOT_A_CANDIDATE')
            rejected.append({'supplier_id': supplier_id, 'code': code if code in _DRAFT_HINTS else 'NOT_A_CANDIDATE'})
            continue
        supplier = _read(lambda: store.get_supplier(context.case_id, supplier_id)) or {}
        recipient = (supplier.get('contact_email') or '').strip()
        if not recipient:
            rejected.append({'supplier_id': supplier_id, 'code': 'NO_CONTACT'})
            continue
        item_name = recipient_item_name(store, case, supplier_id)
        values = {**common, '업체명': supplier.get('name') or '담당자', '품목': item_name,
                  '요청내용': _request_lines(facts | {'품목': item_name}),
                  '질문목록': '\n'.join(f'{n}. {q}' for n, q in enumerate(
                      _questions(candidate['checklist'], _origin_question(chosen.get(supplier_id), candidate.get('origin')),
                                 bool(facts.get('납기일'))), 1))}
        subject, body = _rfq_text(values)
        key = hashlib.sha256(f'{context.case_id}|{context.input_revision}|RFQ|{supplier_id}'.encode()).hexdigest()
        record = MailDraftRecord(
            work_case_id=context.case_id, supplier_id=supplier_id, purpose='RFQ', recipient=recipient,
            subject=subject, body_text=body,
            evidence_refs_jsonb=candidate['evidence_refs'] + (
                [f'agent_runs:{context.agent_run_id}'] if context.agent_run_id else []),
            input_revision=context.input_revision, style_snapshot_jsonb=style, idempotency_key=key)
        saved = store.insert_mail_draft(record, context.input_revision)
        if saved is None:
            return {'status': 'REVISION_CHANGED'}
        drafts.append({'supplier_id': supplier_id, 'draft_id': saved[0]})   # 같은 실행·업체 초안이 있으면 그 ID

    result = {'status': 'DRAFTED' if drafts else 'REJECTED', 'drafts': drafts, 'rejected': rejected}
    if drafts:      # 실제로 초안을 만든 업체 기준의 구매담당자용 문장. 최종 응답 reason에 쓴다
        result['summary'] = search_summary(found, facts.get('품목') or '요청 품목',
                                           [d['supplier_id'] for d in drafts], rejected)
    hints = {item['code']: _DRAFT_HINTS[item['code']] for item in rejected + adjusted}
    if adjusted:    # 기본 선택 질문으로 이미 저장했다
        result['adjusted'] = adjusted
    return result | {'hints': hints} if hints else result


@register
@tool
def create_rfq_draft(supplier_item_ids: list[str], supplier_ids: list[str], runtime: ToolRuntime[RunContext],
                     questions: list[SupplierQuestions] | None = None) -> dict:
    '''업체에 보낼 견적 요청(RFQ) 메일 초안을 저장한다(발송하지 않는다). 메일 문안·받는 주소는 도구가 정한다.
    supplier_item_ids: search_supplier_evidence에 보낸 목록 그대로. 도구가 후보 판정을 다시 확인한다.
    supplier_ids: search_supplier_evidence의 candidates 중 RFQ를 보낼 업체 전부. 한 번에 보낸다.
    questions: 업체마다 고른 선택 질문 [{supplier_id, question_ids}]. question_ids는 question_options의 ID다.
      고정 질문과 미확인 필수 조건 질문은 도구가 늘 넣는다. 빠진 업체는 default_question_options로 묻는다.

    반환 status:
    - DRAFTED: drafts(supplier_id·draft_id), rejected(supplier_id·code: 초안을 만들지 않은 업체),
      summary(실제로 초안을 만든 업체 기준 구매담당자용 문장. 최종 응답 reason에 쓴다),
      adjusted(UNKNOWN_QUESTION: 기본 선택 질문으로 저장한 업체. 다시 보내지 않는다), hints
    - REJECTED: code(NO_SUPPLIERS) 또는 모든 업체가 rejected / CONDITIONS_MISSING(missing) / REVISION_CHANGED /
      CASE_NOT_FOUND
    '''
    return save_mail_drafts(_draft_store, _run_context(_draft_store, runtime), 'RFQ', supplier_item_ids, supplier_ids,
                            questions)


# ─────────────────────────────────────────────
# 공통 계산 (③④⑤ 도구와 구매안·가격 검증 노드가 함께 쓴다. LLM이 직접 부르는 도구가 아니다)
#   설계: docs/AGENTS345_COMMON_DESIGN.md. 견적 행(QuoteVersionRecord)을 업무 수량·단위 기준으로 계산한다.
#   - 금액은 Decimal로 계산한다. 적히지 않은 비용은 0원이 아니라 미확인이다("없음"·"무료"만 0원)
#   - 단가 구간 경계는 원문 표기(이상·초과·미만·이하·까지)대로 적용한다
#   - 단위는 kg·g·톤 사이, 그리고 견적에 박스당 중량이 적혀 있을 때만 박스↔무게로 바꾼다
#   - 세금 기준(TAX_BASES)이 같은 견적끼리만 총액을 비교한다. 다르거나 모르면 비교하지 않는다
# ─────────────────────────────────────────────

_WEIGHT_KG = {'kg': Decimal(1), 'g': Decimal('0.001'), '톤': Decimal(1000)}
# 비용 산정 기준 중 주문(배송) 한 번에 한 번 붙는 것. '회당'·'1회'·'건당' 등에서 '당'·'1'을 뗀 표기
_PER_ORDER = {'회', '건', '주문', '발주', '배송', '납품', '배송회', '주문건'}
# 단위 대조 전에 떼는 앞말 ('1박스당' → '박스당', '매 kg' → 'kg')
_PER_UNIT_PREFIX = re.compile(r'^\s*(?:매\s*)?1?\s*')
# 비교에 꼭 있어야 하는 비용. 견적에 아예 적혀 있지 않으면 미확인으로 본다
REQUIRED_COST_TYPES = ('SHIPPING',)
# 세금 기준. ①이 checks_jsonb.tax_basis에 이 값 중 하나로 저장했을 때만 비교에 쓴다
TAX_BASES = ('INCLUDED', 'EXCLUDED', 'EXEMPT')      # 부가세 포함 / 별도 / 면세
MAX_SPLIT_STEPS = 100_000                           # 배정량 탐색 상한 (업무 수량 ÷ 단위)


def _amount(value) -> Decimal | None:
    """저장된 숫자 문자열을 Decimal로. 비어 있거나 숫자가 아니면 None."""
    if value is None or str(value).strip() == '':
        return None
    try:
        return Decimal(str(value).replace(',', ''))
    except Exception:
        return None


def _mail_quantity(quantity) -> str:
    """메일에 쓰는 수량 표기: 천 단위 쉼표, 끝자리 0 없이 ('1000.000000' → '1,000', '2.5' → '2.5').
    숫자로 읽을 수 없는 값은 그대로 쓴다."""
    try:
        return f'{Decimal(str(quantity)).normalize():,f}'
    except InvalidOperation:
        return str(quantity)


def _amount_text(amount: Decimal) -> str:
    """Decimal을 지수 표기 없는 문자열로. 10000 → '10000', 1.50 → '1.5'."""
    text = format(amount, 'f')
    return text.rstrip('0').rstrip('.') if '.' in text else text


def _unit_key(raw: str | None) -> str | None:
    """단위 표기를 ① 저장 단위로 맞춘다('㎏'·'KG' → 'kg', '박스당' → 'box'). 정해진 단위가 아니면 원문 그대로."""
    if raw is None or not raw.strip():
        return None
    try:
        return _unit(raw, 'unit')
    except ReviewRequired:
        return raw.strip()


def unit_ratio(record: QuoteVersionRecord, from_unit: str | None, to_unit: str | None) -> Decimal | None:
    """from_unit 1단위가 to_unit으로 몇인지. 바꿀 근거가 없으면 None.
    같은 단위, kg·g·톤 사이, 그리고 견적의 '박스당 N kg'(quantity_per_order_unit)이 있을 때 박스↔무게만 바꾼다."""
    if from_unit is None or to_unit is None:
        return None
    if from_unit == to_unit:
        return Decimal(1)
    if from_unit in _WEIGHT_KG and to_unit in _WEIGHT_KG:
        return _WEIGHT_KG[from_unit] / _WEIGHT_KG[to_unit]
    pricing = record.pricing_terms_jsonb or {}
    per_box, per_box_unit = _amount(pricing.get('quantity_per_order_unit')), pricing.get('quantity_per_order_unit_unit')
    if per_box is None or per_box <= 0 or per_box_unit not in _WEIGHT_KG or 'box' not in (from_unit, to_unit):
        return None
    box_kg = per_box * _WEIGHT_KG[per_box_unit]
    if from_unit == 'box' and to_unit in _WEIGHT_KG:
        return box_kg / _WEIGHT_KG[to_unit]
    if to_unit == 'box' and from_unit in _WEIGHT_KG:
        return _WEIGHT_KG[from_unit] / box_kg
    return None


def tax_basis(record: QuoteVersionRecord) -> str | None:
    """견적의 세금 기준(TAX_BASES 중 하나). 정해진 값으로 저장되지 않았으면 None(미확인)."""
    value = ((record.checks_jsonb or {}).get('tax_basis') or {}).get('value')
    return value if value in TAX_BASES else None


def order_limits(record: QuoteVersionRecord, case_unit: str | None) -> dict:
    """업무 단위로 바꾼 주문 조건 {moq, multiple, max}. 적혀 있지 않거나 단위를 바꿀 수 없으면 그 값은 None.
    단위가 없는 MOQ·발주 배수·공급량은 견적 단가 단위로 본다."""
    pricing, quote_unit = record.pricing_terms_jsonb or {}, _unit_key(record.unit)

    def convert(value, unit):
        number = _amount(value)
        ratio = unit_ratio(record, unit or quote_unit, case_unit)
        return None if number is None or ratio is None else number * ratio
    return {'moq': convert(pricing.get('moq'), pricing.get('moq_unit')),
            'multiple': convert(pricing.get('pack_multiple'), pricing.get('pack_multiple_unit')),
            'max': convert(record.available_quantity, pricing.get('available_quantity_unit'))}


def tier_price(tiers: list[dict], quantity: Decimal) -> tuple[Decimal | None, str | None]:
    """견적 단가 단위 수량에 맞는 단가 구간의 단가. 반환: (단가, 사유 코드).
    아래 경계는 '초과'가 아니면 포함, 위 경계는 '이하'·'까지'일 때만 포함한다('미만'·표기 없음은 제외).
    맞는 구간이 없으면 TIER_NOT_COVERED, 단가가 다른 구간이 둘 이상 맞으면 TIER_UNCLEAR."""
    matched = []
    for tier in tiers:
        low, high = _amount(tier.get('min_quantity')), _amount(tier.get('max_quantity'))
        low_raw, high_raw = tier.get('min_quantity_raw') or '', tier.get('max_quantity_raw') or ''
        above = low is None or quantity > low or (quantity == low and '초과' not in low_raw)
        below = high is None or quantity < high or (quantity == high and any(w in high_raw for w in ('이하', '까지')))
        if above and below:
            matched.append(tier)
    if not matched:
        return None, 'TIER_NOT_COVERED'
    if len({tier.get('unit_price') for tier in matched}) > 1:
        return None, 'TIER_UNCLEAR'
    price = _amount(matched[0].get('unit_price'))
    return (price, None) if price is not None else (None, 'NO_PRICE')


@dataclasses.dataclass
class CostSheet:
    """업체 한 곳의 견적을 주어진 수량 기준으로 계산한 결과. 배분 탐색에서 많이 만들어 가벼운 dataclass로 둔다."""
    supplier_id: str
    quote_version_id: str
    quantity: Decimal                            # 업무 단위 수량
    unit_price: Decimal | None = None            # 견적 단가 단위의 적용 단가 (구간이면 해당 구간 단가)
    product_amount: Decimal | None = None        # 상품값 = 적용 단가 × 견적 단위 수량
    known_costs: list[dict] = dataclasses.field(default_factory=list)    # 더한 비용 {cost_type, amount, basis, total}
    unknown_costs: list[dict] = dataclasses.field(default_factory=list)  # 금액을 알 수 없는 비용 {cost_type, code}
    blocked: list[str] = dataclasses.field(default_factory=list)         # 총액을 낼 수 없는 사유 코드
    warnings: list[str] = dataclasses.field(default_factory=list)        # 확인이 필요한 점(SUPPLY_SHORT 등)
    tax_basis: str | None = None

    @property
    def known_total(self) -> Decimal | None:
        """확인된 금액만 더한 총액. 계산을 막는 사유가 있으면 None."""
        if self.blocked or self.product_amount is None:
            return None
        return self.product_amount + sum((cost['total'] for cost in self.known_costs), Decimal(0))


def _cost_line(record: QuoteVersionRecord, cost: dict, quantity: Decimal, case_unit: str | None
               ) -> tuple[Decimal | None, str | None]:
    """비용 한 줄의 금액. 반환: (금액, 미확인 사유 코드).
    단가 포함·'없음'·'무료'는 0, 기준이 없거나 회·건당이면 주문 한 번 금액, 단위당이면 수량을 그 단위로 바꿔 곱한다."""
    if cost.get('included_in_price') is True:
        return Decimal(0), None
    amount = _amount(cost.get('amount'))
    if amount is None:
        return None, 'AMOUNT_UNKNOWN'
    if amount == 0:
        return Decimal(0), None
    if cost.get('included_in_price') is None:
        return None, 'INCLUSION_UNCLEAR'
    if cost.get('basis_type') == 'PER_ORDER':
        return amount, None
    if cost.get('basis_type') == 'PER_UNIT':
        ratio = unit_ratio(record, case_unit, cost.get('basis_unit'))
        return (amount * quantity * ratio, None) if ratio is not None else (None, 'BASIS_UNCLEAR')
    # basis_type이 없는 이전 견적: basis 원문을 키워드로 읽는다
    basis = _compact(cost.get('basis') or '')
    # '1회 배송당'처럼 횟수 표기가 앞에 붙은 기준도 주문(배송) 한 번의 비용으로 본다
    if not basis or (basis.removesuffix('당').removeprefix('1').removeprefix('회') or '회') in _PER_ORDER:
        return amount, None
    ratio = unit_ratio(record, case_unit, _unit_key(basis))
    return (amount * quantity * ratio, None) if ratio is not None else (None, 'BASIS_UNCLEAR')


def _off_multiple(quantity: Decimal, multiple: Decimal | None) -> bool:
    return multiple is not None and multiple > 0 and quantity % multiple != 0


def cost_sheet(quote_version_id: str, record: QuoteVersionRecord, quantity, case_unit: str | None) -> CostSheet:
    """견적 한 행을 업무 단위 수량 기준으로 계산한다. 공급량·MOQ·발주 배수는 계산을 막지 않고 warnings에 남긴다."""
    quantity = _amount(quantity)
    sheet = CostSheet(supplier_id=record.supplier_id, quote_version_id=quote_version_id,
                      quantity=quantity if quantity is not None else Decimal(0), tax_basis=tax_basis(record))
    pricing, case_unit, quote_unit = record.pricing_terms_jsonb or {}, _unit_key(case_unit), _unit_key(record.unit)
    ratio = unit_ratio(record, case_unit, quote_unit)
    if quantity is None or quantity <= 0:
        sheet.blocked.append('QUANTITY_MISSING')
    if quote_unit is None:
        sheet.blocked.append('UNIT_MISSING')
    elif ratio is None:
        sheet.blocked.append('UNIT_MISMATCH')
    if (pricing.get('currency') or record.currency or 'KRW') != 'KRW' or \
            any((tier.get('currency') or 'KRW') != 'KRW' for tier in pricing.get('price_tiers') or []):
        sheet.blocked.append('FOREIGN_CURRENCY')
    if sheet.blocked:
        return sheet

    quote_quantity = quantity * ratio
    if pricing.get('price_tiers'):
        price, code = tier_price(pricing['price_tiers'], quote_quantity)
    else:
        price = _amount(record.unit_price)
        code = None if price is not None else 'NO_PRICE'
    if code:
        sheet.blocked.append(code)
        return sheet
    sheet.unit_price, sheet.product_amount = price, price * quote_quantity

    costs = record.cost_terms_jsonb or []
    for cost in costs:
        total, code = _cost_line(record, cost, quantity, case_unit)
        if code:
            sheet.unknown_costs.append({'cost_type': cost.get('cost_type'), 'code': code})
        elif total:
            sheet.known_costs.append({'cost_type': cost.get('cost_type'), 'amount': cost.get('amount'),
                                      'basis': cost.get('basis'), 'total': total})
    stated = {cost.get('cost_type') for cost in costs}
    sheet.unknown_costs += [{'cost_type': kind, 'code': 'NOT_STATED'} for kind in REQUIRED_COST_TYPES
                            if kind not in stated]
    limits = order_limits(record, case_unit)
    if limits['max'] is not None and limits['max'] < quantity:
        sheet.warnings.append('SUPPLY_SHORT')
    if limits['moq'] is not None and quantity < limits['moq']:
        sheet.warnings.append('BELOW_MOQ')
    if _off_multiple(quantity, limits['multiple']):
        sheet.warnings.append('NOT_MULTIPLE')
    return sheet


def plan_total(parts: list[tuple[str, QuoteVersionRecord, Decimal]], case_quantity, case_unit: str | None,
               policy: dict | None = None) -> dict:
    """구매안(업체별 배정량)의 총액과 위반. parts: [(견적 버전 ID, 견적 행, 업무 단위 배정량)].
    위반은 {code, supplier_id}: 업체 수(MAX_SUPPLIERS·SPLIT_NOT_ALLOWED), 배정 합계(SUM_MISMATCH),
    공급량·MOQ·발주 배수(SUPPLY_SHORT·BELOW_MOQ·NOT_MULTIPLE), 계산 불가(BLOCKED), 세금 기준(TAX_BASIS_MIXED·UNKNOWN)."""
    policy = policy or {}
    sheets = [cost_sheet(quote_id, record, quantity, case_unit) for quote_id, record, quantity in parts]
    violations = []
    suppliers = {sheet.supplier_id for sheet in sheets}
    if len(suppliers) != len(sheets):
        violations.append({'code': 'DUPLICATE_SUPPLIER', 'supplier_id': None})
    if len(suppliers) > int(policy.get('max_suppliers') or 2):
        violations.append({'code': 'MAX_SUPPLIERS', 'supplier_id': None})
    if len(suppliers) > 1 and policy.get('allow_split') is False:
        violations.append({'code': 'SPLIT_NOT_ALLOWED', 'supplier_id': None})
    if sum((sheet.quantity for sheet in sheets), Decimal(0)) != _amount(case_quantity):
        violations.append({'code': 'SUM_MISMATCH', 'supplier_id': None})
    for sheet in sheets:
        violations += [{'code': code, 'supplier_id': sheet.supplier_id} for code in sheet.warnings]
        violations += [{'code': 'BLOCKED', 'supplier_id': sheet.supplier_id, 'reasons': sheet.blocked}] \
            if sheet.blocked else []
    bases = {sheet.tax_basis for sheet in sheets}
    if None in bases:
        violations.append({'code': 'TAX_BASIS_UNKNOWN', 'supplier_id': None})
    elif len(bases) > 1:
        violations.append({'code': 'TAX_BASIS_MIXED', 'supplier_id': None})
    totals = [sheet.known_total for sheet in sheets]
    known_total = None if None in totals else sum(totals, Decimal(0))
    return {'sheets': sheets, 'known_total': known_total, 'violations': violations,
            'unknown_costs': [{'supplier_id': s.supplier_id, **cost} for s in sheets for cost in s.unknown_costs]}


def _split_quantities(first: QuoteVersionRecord, second: QuoteVersionRecord, case_quantity: Decimal,
                      case_unit: str | None) -> list[tuple[Decimal, Decimal]]:
    """두 업체가 합계 case_quantity를 나눠 맡을 수 있는 배정량 (첫 업체, 둘째 업체) 목록.
    각 업체가 MOQ 이상·공급량 이하·발주 배수에 맞아야 한다. 발주 배수가 없는 업체는 업무 단위 1씩이다."""
    a, b = order_limits(first, case_unit), order_limits(second, case_unit)
    step = b['multiple'] if b['multiple'] and b['multiple'] > 0 else Decimal(1)
    low = max(b['moq'] or Decimal(0), step)
    high = min(case_quantity - (a['moq'] or Decimal(0)), b['max'] if b['max'] is not None else case_quantity)
    found, quantity = [], (low / step).to_integral_value(rounding='ROUND_CEILING') * step
    for _ in range(MAX_SPLIT_STEPS):
        if quantity > high or quantity >= case_quantity:
            break
        rest = case_quantity - quantity
        if (a['max'] is None or rest <= a['max']) and not _off_multiple(rest, a['multiple']):
            found.append((rest, quantity))
        quantity += step
    return found


def adjust_split(first: tuple[str, QuoteVersionRecord], second: tuple[str, QuoteVersionRecord], wanted_first,
                 case_quantity, case_unit: str | None) -> dict:
    """LLM이 정한 첫 업체 배정량(wanted_first)에 가장 가까운 가능한 배분으로 맞춘다. 가까운 후보가 둘이면 총액이 낮은 쪽.
    반환: {status: ADJUSTED·UNCHANGED·NO_FEASIBLE_SPLIT, quantities: (첫, 둘째), wanted: (첫, 둘째)}"""
    case_quantity, wanted = _amount(case_quantity), _amount(wanted_first)
    options = _split_quantities(first[1], second[1], case_quantity, case_unit)
    result = {'wanted': (wanted, case_quantity - wanted)}
    if not options:
        return result | {'status': 'NO_FEASIBLE_SPLIT', 'quantities': None}

    def total(option):
        value = plan_total([(first[0], first[1], option[0]), (second[0], second[1], option[1])],
                           case_quantity, case_unit)['known_total']
        return value if value is not None else Decimal('Infinity')
    best = min(options, key=lambda option: (abs(option[0] - wanted), total(option)))
    return result | {'status': 'UNCHANGED' if best[0] == wanted else 'ADJUSTED', 'quantities': best}


def cheapest_split(first: tuple[str, QuoteVersionRecord], second: tuple[str, QuoteVersionRecord], case_quantity,
                   case_unit: str | None) -> dict:
    """두 업체 조합에서 가능한 배분 중 확인된 총액이 가장 낮은 것. 미확인 비용은 총액에 넣지 않고 함께 돌려준다.
    반환: {status: FOUND·NO_FEASIBLE_SPLIT·NOT_COMPUTABLE, quantities, known_total, searched(살펴본 배분 수)}"""
    case_quantity = _amount(case_quantity)
    options = _split_quantities(first[1], second[1], case_quantity, case_unit)
    if not options:
        return {'status': 'NO_FEASIBLE_SPLIT', 'quantities': None, 'known_total': None, 'searched': 0}
    priced = []
    for option in options:
        plan = plan_total([(first[0], first[1], option[0]), (second[0], second[1], option[1])], case_quantity,
                          case_unit)
        if plan['known_total'] is not None:
            priced.append((plan['known_total'], option))
    if not priced:
        return {'status': 'NOT_COMPUTABLE', 'quantities': None, 'known_total': None, 'searched': len(options)}
    total, option = min(priced, key=lambda item: item[0])
    return {'status': 'FOUND', 'quantities': option, 'known_total': total, 'searched': len(options)}


def break_even(target: CostSheet, rival: CostSheet) -> dict:
    """역전 기준 (Notion 07 ③): target의 미확인 비용이 얼마 미만이면 target이 rival보다 싸지는지.
    기준 금액 = rival 확인 총액 − target 확인 총액.
    status: COMPUTED(rival 총액 확정) / CONDITIONAL(rival에도 미확인) / NO_UNKNOWN(target에 미확인 없음) /
    NOT_COMPUTABLE(어느 한쪽 총액 불가) / TAX_BASIS_DIFFERENT(세금 기준이 다르거나 모름)
    can_change_choice: 미확인 비용의 답에 따라 더 싼 쪽이 바뀔 수 있는지."""
    result = {'target_supplier_id': target.supplier_id, 'target_quote_version_id': target.quote_version_id,
              'rival_supplier_id': rival.supplier_id, 'rival_quote_version_id': rival.quote_version_id,
              'target_unknown_costs': target.unknown_costs, 'rival_unknown_costs': rival.unknown_costs,
              'warnings': {'target': target.warnings, 'rival': rival.warnings}}
    if target.known_total is None or rival.known_total is None:
        return result | {'status': 'NOT_COMPUTABLE', 'can_change_choice': None,
                         'reasons': {'target': target.blocked, 'rival': rival.blocked}}
    if target.tax_basis is None or target.tax_basis != rival.tax_basis:
        return result | {'status': 'TAX_BASIS_DIFFERENT', 'can_change_choice': None,
                         'tax_basis': {'target': target.tax_basis, 'rival': rival.tax_basis}}
    target_total, rival_total = target.known_total, rival.known_total
    amount = rival_total - target_total
    difference = f'{_amount_text(rival_total)} - {_amount_text(target_total)} = {_amount_text(amount)}'
    totals = {'target_known_total': _amount_text(target_total), 'rival_known_total': _amount_text(rival_total),
              'break_even_amount': _amount_text(amount)}
    if not target.unknown_costs:
        return result | totals | {'status': 'NO_UNKNOWN', 'can_change_choice': bool(rival.unknown_costs),
                                  'formula': difference}
    target_terms = '+'.join(cost['cost_type'] for cost in target.unknown_costs)
    formula = f'{difference}; target 미확인({target_terms}) < {_amount_text(amount)}'
    if rival.unknown_costs:
        rival_terms = '+'.join(cost['cost_type'] for cost in rival.unknown_costs)
        return result | totals | {'status': 'CONDITIONAL', 'can_change_choice': True,
                                  'formula': f'{formula} + rival 미확인({rival_terms})'}
    return result | totals | {'status': 'COMPUTED', 'can_change_choice': amount > 0,
                              'formula': f'{formula} 이면 target이 더 싸다'}


def keep_price_ceiling(incumbent: CostSheet, record: QuoteVersionRecord, alternative_total: Decimal,
                       alternative_tax_basis: str | None) -> dict:
    """유지 상한 단가 (⑤): 기존 업체가 견적 단가 단위로 이 단가 이하면 검증된 대안 총액보다 싸거나 같다.
    상한 = (대안 총액 − 기존 업체의 단가 외 확인 비용) ÷ 견적 단위 수량.
    status: COMPUTED / CONDITIONAL(기존 업체에 미확인 비용) / NOT_COMPUTABLE / TAX_BASIS_DIFFERENT"""
    if incumbent.known_total is None or incumbent.unit_price is None:
        return {'status': 'NOT_COMPUTABLE', 'reasons': incumbent.blocked}
    if incumbent.tax_basis is None or incumbent.tax_basis != alternative_tax_basis:
        return {'status': 'TAX_BASIS_DIFFERENT',
                'tax_basis': {'incumbent': incumbent.tax_basis, 'alternative': alternative_tax_basis}}
    quote_quantity = incumbent.product_amount / incumbent.unit_price
    other_costs = incumbent.known_total - incumbent.product_amount
    ceiling = (_amount(alternative_total) - other_costs) / quote_quantity
    return {'status': 'CONDITIONAL' if incumbent.unknown_costs else 'COMPUTED',
            'ceiling_unit_price': _amount_text(ceiling), 'unit': _unit_key(record.unit),
            'current_unit_price': _amount_text(incumbent.unit_price), 'unknown_costs': incumbent.unknown_costs,
            'formula': f'({_amount_text(_amount(alternative_total))} - {_amount_text(other_costs)}) / '
                       f'{_amount_text(quote_quantity)} = {_amount_text(ceiling)}'}



# ─────────────────────────────────────────────
# ③ INFO_PRIORITY: check_comparability(③④⑤ 공통), calculate_break_even, create_clarification_draft
#   설계: docs/AGENT3_PRIORITY_DESIGN.md. 업무의 최신 견적을 업무 수량 기준으로 비교해 필수 누락·미확인 비용을 찾고,
#   미확인 비용은 역전 기준으로 선택이 바뀔 수 있는지 계산한다. 질문 문장·메일 문안은 코드가 정한다.
# ─────────────────────────────────────────────

class ComparisonStore(CaseStore, Protocol):
    """check_comparability·calculate_break_even의 저장소 (06 quote_versions)."""

    def get_quote_versions(self, case_id: str, ids: list[str]) -> list[tuple[str, QuoteVersionRecord]]:
        """이 업무의 quote_versions 중 id가 ids에 있는 (id, 행). 범위 밖 id는 빠진다."""


class PriorityStore(ComparisonStore, MailDraftStore, Protocol):
    """③ 도구 전체의 저장소. 보완 요청 초안은 mail_drafts에 저장한다."""


_priority_store: PriorityStore | None = None


def use_priority_store(store: PriorityStore | None) -> None:
    global _priority_store
    _priority_store = store


# 총액을 막는 사유 → 업체에 물을 견적 항목 (①이 다시 추출할 때도 같은 이름을 쓴다)
_PRICE_BLOCK_FIELDS = {'NO_PRICE': 'unit_price', 'UNIT_MISSING': 'unit', 'UNIT_MISMATCH': 'unit',
                       'TIER_NOT_COVERED': 'price_tiers', 'TIER_UNCLEAR': 'price_tiers', 'FOREIGN_CURRENCY': 'currency'}
COST_LABELS = {'SHIPPING': '운송비', 'HANDLING': '하차비', 'OTHER': '기타 비용'}
# 보완 요청 질문 문장. 비용은 'cost:<cost_type>' 필드로 묻는다
CLARIFY_QUESTIONS = {'specification': '규격(등급·중량·포장 단위)', 'certifications': '인증 보유 여부와 증빙',
                     'available_quantity': '요청 수량 공급 가능 여부와 최대 공급량',
                     'delivery_date': '요청 납기일 납품 가능 여부(또는 발주 후 납품까지 걸리는 기간)',
                     'unit_price': '단가', 'unit': '단가 기준 단위(예: kg당, 박스당)와 박스당 중량',
                     'price_tiers': '요청 수량에 적용되는 단가', 'currency': '원화 기준 단가',
                     'tax_terms': '단가의 부가세 포함·별도·면세 여부'}


def _cost_field(cost_type: str) -> str:
    return f'cost:{cost_type}'


def clarify_question(field: str) -> str | None:
    """보완 요청 질문 한 줄. 모르는 필드면 None."""
    if field.startswith('cost:'):
        label = COST_LABELS.get(field.removeprefix('cost:'))
        return f'{label} 금액, 산정 기준(회당·박스당 등), 단가 포함 여부' if label else None
    return CLARIFY_QUESTIONS.get(field)


def _requirement(name: str, field: str, required: str, stated: str, met: bool) -> dict:
    """필수 조건 한 줄. 문자열로 충족이 확인되면 MET, 아니면 LLM이 원문을 읽고 판단할 CHECK."""
    return {'requirement': name, 'field': field, 'required': required, 'stated': stated, 'state': 'MET' if met else 'CHECK'}


def _supplier_comparison(case: dict, quote_id: str, record: QuoteVersionRecord, incumbents: set[str]) -> dict:
    """업체 한 곳의 비교 상태: 업무 수량 기준 계산, 필수 항목 누락(07 ③-1), 충족 판단이 필요한 필수 조건."""
    sheet = cost_sheet(quote_id, record, case['quantity'], case['unit'])
    checks = record.checks_jsonb or {}
    stated = lambda name: ((checks.get(name) or {}).get('value') or '').strip() or None
    missing, requirements = [], []
    if case.get('specification'):
        if stated('specification') is None:
            missing.append('specification')
        else:
            requirements.append(_requirement('규격', 'specification', case['specification'], stated('specification'),
                                             _letters(case['specification']) == _letters(stated('specification'))))
    certifications = _required_names(case.get('mandatory_conditions'))
    if certifications and stated('certifications') is None:
        missing.append('certifications')
    owned = _cert_keys([stated('certifications') or ''])
    requirements += [_requirement(name, 'certifications', name, stated('certifications'), _cert_key(name) in owned)
                     for name in certifications if stated('certifications') is not None]
    if record.available_quantity is None:
        missing.append('available_quantity')
    if case.get('due_date') and stated('delivery_date') is None and stated('lead_time') is None:
        missing.append('delivery_date')
    missing += list(dict.fromkeys(_PRICE_BLOCK_FIELDS[code] for code in sheet.blocked if code in _PRICE_BLOCK_FIELDS))
    if sheet.tax_basis is None:
        missing.append('tax_terms')
    return {'supplier_id': record.supplier_id, 'quote_version_id': quote_id,
            'is_incumbent': record.supplier_id in incumbents,
            'known_total': _amount_text(sheet.known_total) if sheet.known_total is not None else None,
            'blocked': sheet.blocked, 'warnings': sheet.warnings, 'tax_basis': sheet.tax_basis,
            'unknown_costs': [{'field': _cost_field(cost['cost_type']), **cost} for cost in sheet.unknown_costs],
            'missing_required': missing, 'requirements': requirements}


def _priority_case(store: ComparisonStore, context: RunContext) -> tuple[dict | None, dict | None]:
    """업무 조건과 최신 견적 (case, None) 또는 멈출 결과 (None, {status})."""
    case = _read(lambda: store.get_work_case(context.case_id))
    if case is None:
        return None, {'status': 'CASE_NOT_FOUND'}
    if case.get('input_revision') != context.input_revision:
        return None, {'status': 'REVISION_CHANGED'}
    missing = [name for name in ('quantity', 'unit') if not case.get(name)]
    if missing:
        return None, {'status': 'CONDITIONS_MISSING', 'missing': missing}
    return case, None


def compare_quotes(store: ComparisonStore, context: RunContext) -> dict:
    """check_comparability의 본체. 업무의 최신 견적 전부를 업무 수량 기준으로 비교한다."""
    case, stop = _priority_case(store, context)
    if stop:
        return stop
    incumbents = {mail['supplier_id'] for mail in _read(lambda: store.list_case_mails(context.case_id))
                  if mail.get('message_role') == 'INITIAL' and mail.get('supplier_id')}
    quotes = _read(lambda: store.get_quote_versions(context.case_id, case.get('latest_quote_version_ids') or []))
    suppliers = [_supplier_comparison(case, quote_id, record, incumbents) for quote_id, record in quotes]
    # 다음 할 일: 필수 누락·미확인 비용·판단할 규격·인증은 먼저 원문으로 확인하고, 원문에도 금액이 없는 비용만
    # 역전 기준으로 판단한다(07 ③-2·③-3). 견적에서 빠졌을 뿐 원문에 있는 비용을 업체에 다시 묻지 않기 위해서다.
    break_even_needed = [s['supplier_id'] for s in suppliers if s['unknown_costs']]
    read_source_needed = [s['supplier_id'] for s in suppliers if s['missing_required'] or s['unknown_costs']
                          or any(r['state'] == 'CHECK' for r in s['requirements'])]
    next_steps = [f'read_mail_evidence로 원문을 확인한다: {", ".join(read_source_needed)} (필수 누락·미확인 비용·판단할 조건)'] \
        if read_source_needed else []
    next_steps += [f'원문에도 금액이 없는 비용만 calculate_break_even을 부른다: {", ".join(break_even_needed)}'] \
        if break_even_needed else []
    next_steps += ['원문에 답이 있는 누락은 recheck에 원문 구절을 인용해 RECHECK_SOURCE로 넘긴다(업체에 묻지 않는다)',
                   '업체에 물을 질문이 있으면 create_clarification_draft를 한 번 부르고 REQUEST_CLARIFICATION으로 끝낸다'] \
        if break_even_needed or read_source_needed else []
    return {'status': 'OK', 'quantity': str(case['quantity']), 'unit': case['unit'], 'suppliers': suppliers,
            'comparable_count': sum(1 for s in suppliers if s['known_total'] is not None and s['tax_basis']),
            'tax_bases': sorted({s['tax_basis'] for s in suppliers if s['tax_basis']}),
            'break_even_needed': break_even_needed, 'read_source_needed': read_source_needed, 'next_steps': next_steps}


@register
@tool
def check_comparability(runtime: ToolRuntime[RunContext]) -> dict:
    '''업무의 최신 견적을 업무 수량 기준으로 비교할 수 있는지 업체별로 확인한다. 인자는 없다.

    반환 status:
    - OK: suppliers(업체별 supplier_id·quote_version_id·is_incumbent(인상을 요청한 업체)·known_total(확인된 금액만 더한
      총액, 계산 불가면 null)·blocked(총액을 막는 사유)·unknown_costs(금액을 모르는 비용 field·cost_type·code)·
      missing_required(견적에 없는 필수 항목 field)·requirements(견적에 적힌 규격·인증과 업무 조건. state MET이면 충족,
      CHECK이면 원문을 보고 충족 여부를 판단한다)·tax_basis·warnings), comparable_count, tax_bases,
      break_even_needed·read_source_needed·next_steps(이어서 할 일: 필수 누락·미확인 비용은 원문 확인, 원문에도 없는 비용은 역전 기준)
    - CONDITIONS_MISSING(업무 수량·단위 없음) / REVISION_CHANGED / CASE_NOT_FOUND
    '''
    return compare_quotes(_priority_store, _run_context(_priority_store, runtime))


def break_even_for(store: ComparisonStore, context: RunContext, supplier_id: str) -> dict:
    """calculate_break_even의 본체. supplier_id의 미확인 비용을 다른 업체 각각과 비교한다.
    deciding: 확인 총액이 가장 낮은 다른 업체와의 결과 (질문의 역전 기준 금액·산식으로 쓴다).
    업체 하나가 업무 수량 전부를 맡는 비교라서, 공급 가능 수량이 업무 수량보다 적은 업체(SUPPLY_SHORT)는 상대로 두지 않는다."""
    case, stop = _priority_case(store, context)
    if stop:
        return stop
    quotes = _read(lambda: store.get_quote_versions(context.case_id, case.get('latest_quote_version_ids') or []))
    sheets = {record.supplier_id: cost_sheet(quote_id, record, case['quantity'], case['unit']) for quote_id, record in quotes}
    if supplier_id not in sheets:
        return {'status': 'REJECTED', 'code': 'NOT_IN_COMPARISON', 'hint': 'check_comparability의 supplier_id만 보낸다.'}
    target = sheets[supplier_id]
    rivals = sorted((sheet for sid, sheet in sheets.items() if sid != supplier_id and 'SUPPLY_SHORT' not in sheet.warnings),
                    key=lambda sheet: (sheet.known_total is None, sheet.known_total or 0))
    results = [break_even(target, rival) for rival in rivals]
    computed = [r for r in results if r['status'] in ('COMPUTED', 'NO_UNKNOWN', 'CONDITIONAL')]
    return {'status': 'OK', 'target_supplier_id': supplier_id, 'results': results,
            'deciding': computed[0] if computed else None,
            'can_change_choice': any(r['can_change_choice'] is True for r in results),
            # 추가 조사 실익 낮음의 입증: 비교 가능한 업체가 있고, 모든 비교에서 답과 관계없이 선택이 바뀌지 않는다(07 ③-5)
            'proven_low_value': bool(computed) and len(computed) == len(results)
            and all(r['can_change_choice'] is False for r in results)}


@register
@tool
def calculate_break_even(supplier_id: str, runtime: ToolRuntime[RunContext]) -> dict:
    '''업체(supplier_id)의 미확인 비용이 얼마 미만이면 다른 업체보다 싸지는지(역전 기준)를 다른 업체 각각과 계산한다.
    업체 하나가 업무 수량 전부를 맡는 경우끼리 비교한다.

    반환 status:
    - OK: results(다른 업체별 status·break_even_amount·can_change_choice·formula), deciding(가장 싼 다른 업체와의 결과),
      can_change_choice(답에 따라 더 싼 쪽이 바뀔 수 있으면 true), proven_low_value(모든 비교에서 바뀌지 않음이
      계산으로 확인되면 true: 묻지 않아도 된다)
      결과의 status: COMPUTED / CONDITIONAL(상대에도 미확인 비용) / NO_UNKNOWN / NOT_COMPUTABLE /
      TAX_BASIS_DIFFERENT(세금 기준이 다르거나 몰라 비교하지 않음)
    - REJECTED(NOT_IN_COMPARISON) / CONDITIONS_MISSING / REVISION_CHANGED / CASE_NOT_FOUND
    '''
    return break_even_for(_priority_store, _run_context(_priority_store, runtime), supplier_id)


class ClarificationRequest(StrictModel):
    supplier_id: str
    fields: list[str] = Field(min_length=1)    # check_comparability의 missing_required·unknown_costs·requirements의 field


CLARIFY_SUBJECT = '{보내는회사}{품목} 견적 확인 요청드립니다'
CLARIFY_BODY = ('안녕하세요. {업체명} 담당자님\n'
                '{회사소개}\n\n'
                '보내 주신 {품목} 견적 잘 받았습니다. 검토를 위해 아래 내용을 {회신기한}에 확인 부탁드립니다.\n\n'
                '<확인 요청 사항>\n'
                '{질문목록}\n\n'
                '감사합니다.\n'
                '{서명}')
_CLARIFY_HINTS = {
    'NOT_IN_COMPARISON': 'check_comparability의 supplier_id에만 보완 요청을 만든다.',
    'FIELD_NOT_OPEN': 'fields는 그 업체의 missing_required·unknown_costs·requirements의 field만 쓴다.',
    'NO_CONTACT': '연락처가 없는 업체다. 이 업체는 건너뛴다.',
}


def save_clarification_drafts(store: PriorityStore, context: RunContext, requests: list[ClarificationRequest]) -> dict:
    """create_clarification_draft의 본체. 업체별 질문을 코드 템플릿 메일 초안으로 저장한다."""
    compared = compare_quotes(store, context)
    if compared['status'] != 'OK':
        return {'status': compared['status']}
    suppliers = {s['supplier_id']: s for s in compared['suppliers']}
    case = (_read(lambda: store.get_work_case(context.case_id)) or {}) | {'id': context.case_id}
    style = _read(lambda: store.current_send_settings(context.case_id)) or {}
    common = {'회신기한': REPLY_DEADLINE, **_style_values(style)}
    drafts, rejected = [], []
    for request in requests:
        supplier = suppliers.get(request.supplier_id)
        if supplier is None:
            rejected.append({'supplier_id': request.supplier_id, 'code': 'NOT_IN_COMPARISON'})
            continue
        open_fields = set(supplier['missing_required']) | {c['field'] for c in supplier['unknown_costs']} | {
            r['field'] for r in supplier['requirements'] if r['state'] == 'CHECK'}
        fields = list(dict.fromkeys(request.fields))
        if not set(fields) <= open_fields:
            rejected.append({'supplier_id': request.supplier_id, 'code': 'FIELD_NOT_OPEN',
                             'fields': sorted(set(fields) - open_fields)})
            continue
        contact = _read(lambda: store.get_supplier(context.case_id, request.supplier_id)) or {}
        recipient = (contact.get('contact_email') or '').strip()
        if not recipient:
            rejected.append({'supplier_id': request.supplier_id, 'code': 'NO_CONTACT'})
            continue
        lines = '\n'.join(f'{n}. {clarify_question(field)}' for n, field in enumerate(fields, 1))
        subject, body = _mail_text(CLARIFY_SUBJECT, CLARIFY_BODY,
                                   {**common, '업체명': contact.get('name') or '담당자', '질문목록': lines,
                                    '품목': recipient_item_name(store, case, request.supplier_id)})
        key = hashlib.sha256(
            f'{context.case_id}|{context.input_revision}|CLARIFICATION|{request.supplier_id}'.encode()).hexdigest()
        record = MailDraftRecord(
            work_case_id=context.case_id, supplier_id=request.supplier_id, purpose='CLARIFICATION', recipient=recipient,
            subject=subject, body_text=body,
            evidence_refs_jsonb=[f"quote_versions:{supplier['quote_version_id']}"] + (
                [f'agent_runs:{context.agent_run_id}'] if context.agent_run_id else []),
            input_revision=context.input_revision, style_snapshot_jsonb=style, idempotency_key=key)
        saved = store.insert_mail_draft(record, context.input_revision)
        if saved is None:
            return {'status': 'REVISION_CHANGED'}
        drafts.append({'supplier_id': request.supplier_id, 'draft_id': saved[0], 'fields': fields})
    result = {'status': 'DRAFTED' if drafts else 'REJECTED', 'drafts': drafts, 'rejected': rejected}
    hints = {item['code']: _CLARIFY_HINTS[item['code']] for item in rejected}
    return result | {'hints': hints} if hints else result


@register
@tool
def create_clarification_draft(requests: list[ClarificationRequest], runtime: ToolRuntime[RunContext]) -> dict:
    '''업체에 보낼 보완 요청 메일 초안을 저장한다(발송하지 않는다). 메일 문안·질문 문장·받는 주소는 도구가 정한다.
    requests: 업체마다 물을 항목 [{supplier_id, fields}]. 한 번에 모든 업체를 보낸다.
      fields는 check_comparability 결과의 그 업체 missing_required·unknown_costs·requirements의 field다
      (예: 'available_quantity', 'tax_terms', 'cost:SHIPPING').

    반환 status:
    - DRAFTED: drafts(supplier_id·draft_id·fields), rejected(supplier_id·code), hints
    - REJECTED: 모든 업체가 rejected / CONDITIONS_MISSING / REVISION_CHANGED / CASE_NOT_FOUND
    '''
    return save_clarification_drafts(_priority_store, _run_context(_priority_store, runtime), requests)



# ─────────────────────────────────────────────
# ④ RESPONSE_PLAN: build_plan_candidates, validate_purchase_plan (+ 구매안 검증 노드의 revalidate_plans)
#   설계: docs/AGENT4_PLAN_DESIGN.md. 확인된 업체로 유지(KEEP)·전환(SWITCH)·최대 2곳 분산(SPLIT) 안을 코드가 만들고 검증한다.
#   필수 조건 미충족·불명확, 미확인 비용, 필수 누락이 남은 업체는 뺀다. 기존 업체만 예외로 유지안을 남기고 그 사유를
#   성립 조건으로 단다(CONDITIONAL). 규격·인증 중 판단이 필요한 항목(CHECK)은 LLM이 충족이라고 넘긴 것만 충족이다.
# ─────────────────────────────────────────────

MAX_PLAN_OPTIONS = 3                 # build_plan_candidates가 돌려주는 전환·분산안 수 (각각 총액이 낮은 순)


class MetRequirement(StrictModel):
    supplier_id: str
    requirement: str                   # check_comparability requirements의 requirement (예: '규격', 'HACCP')


class PlanAllocationInput(StrictModel):
    supplier_id: str
    quantity: str | None = None        # 업무 단위 배정량. KEEP·SWITCH와 mode=cheapest면 비워도 된다


def _won(amount: Decimal | None) -> str | None:
    return _amount_text(amount) if amount is not None else None


def _plan_suppliers(store: ComparisonStore, context: RunContext, met: list[MetRequirement]) -> tuple[dict | None, dict]:
    """업체별 구매안 자격. 반환: (업무, {supplier_id: {quote_id, record, is_incumbent, eligible, reasons, conditions}})
    또는 (None, 멈출 결과)."""
    compared = compare_quotes(store, context)
    if compared['status'] != 'OK':
        return None, compared
    case = _read(lambda: store.get_work_case(context.case_id))
    records = dict(_read(lambda: store.get_quote_versions(context.case_id, case.get('latest_quote_version_ids') or [])))
    accepted = {(m.supplier_id, m.requirement) for m in met}
    suppliers = {}
    for item in compared['suppliers']:
        sid = item['supplier_id']
        unclear = [r['requirement'] for r in item['requirements']
                   if r['state'] == 'CHECK' and (sid, r['requirement']) not in accepted]
        reasons = ([f"총액 계산 불가({', '.join(item['blocked'])})"] if item['blocked'] else []) + \
            (['세금 기준 미확인'] if item['tax_basis'] is None else []) + \
            ([f"필수 항목 누락({', '.join(item['missing_required'])})"] if item['missing_required'] else []) + \
            ([f"필수 조건 미충족·불명확({', '.join(unclear)})"] if unclear else []) + \
            ([f"미확인 비용({', '.join(COST_LABELS.get(c['cost_type'], c['cost_type']) for c in item['unknown_costs'])})"]
             if item['unknown_costs'] else [])
        # 기존 업체는 총액을 낼 수 있으면 유지안을 남기고 사유를 성립 조건으로 단다(비교 기준·⑤에 필요)
        incumbent_kept = item['is_incumbent'] and not item['blocked']
        suppliers[sid] = {'quote_id': item['quote_version_id'], 'record': records[item['quote_version_id']],
                          'is_incumbent': item['is_incumbent'], 'eligible': not reasons or incumbent_kept,
                          'reasons': reasons, 'conditions': reasons if incumbent_kept else []}
    return case, suppliers


def _evaluate_plan(case: dict, suppliers: dict, plan_type: str, parts: list[tuple[str, Decimal]]) -> dict:
    """업체별 배정량 parts로 안 하나를 계산·검증한다. status: VALID / CONDITIONAL / INVALID."""
    total = plan_total([(suppliers[sid]['quote_id'], suppliers[sid]['record'], quantity) for sid, quantity in parts],
                       case['quantity'], case['unit'], case.get('policy') or {})
    # 업체 하나가 전량을 맡는 안은 안 안에서 세금 기준을 맞출 상대가 없다
    violations = [v for v in total['violations'] if not (len(parts) == 1 and v['code'] == 'TAX_BASIS_UNKNOWN')]
    violations += [{'code': 'NOT_ELIGIBLE', 'supplier_id': sid, 'reasons': suppliers[sid]['reasons']}
                   for sid, _ in parts if not suppliers[sid]['eligible']]
    conditions = [f"{sid}: {reason}" for sid, _ in parts for reason in suppliers[sid]['conditions']]
    status = 'INVALID' if violations or total['known_total'] is None else 'CONDITIONAL' if conditions else 'VALID'
    return {'status': status, 'plan_type': plan_type, 'total_amount': _won(total['known_total']),
            'allocations': [{'supplier_id': sid, 'quote_version_id': suppliers[sid]['quote_id'],
                             'quantity': _amount_text(quantity)} for sid, quantity in parts],
            'conditions': conditions, 'violations': violations,
            'tax_basis': sorted({sheet.tax_basis or '미확인' for sheet in total['sheets']})}


def _cheaper_than_single(split: dict, singles: list[dict]) -> bool:
    """분산안이 두 업체 중 전량을 맡을 수 있는 쪽의 전량안보다 싼지. 한 업체가 전량을 맡을 수 있는데 분산이 더 싸지 않으면
    그 분산은 사실상 전환안(예: D 999 + A 1)이라 보이지 않는다."""
    totals = [Decimal(p['total_amount']) for p in singles if p['status'] != 'INVALID' and p['total_amount'] is not None]
    return split['total_amount'] is not None and all(Decimal(split['total_amount']) < total for total in totals)


def plan_candidates(store: ComparisonStore, context: RunContext, met: list[MetRequirement]) -> dict:
    """build_plan_candidates의 본체. 유지안, 전환안·분산안(각각 총액이 낮은 MAX_PLAN_OPTIONS개)과 만들지 못한 이유."""
    case, suppliers = _plan_suppliers(store, context, met)
    if case is None:
        return suppliers
    quantity, policy = _amount(case['quantity']), case.get('policy') or {}
    eligible = [sid for sid, s in suppliers.items() if s['eligible']]
    incumbents = [sid for sid in eligible if suppliers[sid]['is_incumbent']]
    keep = [_evaluate_plan(case, suppliers, 'KEEP', [(sid, quantity)]) for sid in incumbents]
    switches = [_evaluate_plan(case, suppliers, 'SWITCH', [(sid, quantity)])
                for sid in eligible if not suppliers[sid]['is_incumbent']]
    splits, pairs, not_split = [], 0, []
    if policy.get('allow_split') is not False and int(policy.get('max_suppliers') or 2) >= 2:
        for index, first in enumerate(eligible):
            for second in eligible[index + 1:]:
                pairs += 1
                found = cheapest_split((suppliers[first]['quote_id'], suppliers[first]['record']),
                                       (suppliers[second]['quote_id'], suppliers[second]['record']), quantity, case['unit'])
                if found['status'] != 'FOUND':
                    not_split.append({'supplier_ids': [first, second], 'code': found['status']})
                    continue
                plan = _evaluate_plan(case, suppliers, 'SPLIT', [(first, found['quantities'][0]),
                                                                 (second, found['quantities'][1])])
                if not _cheaper_than_single(plan, [p for p in keep + switches if p['allocations'][0]['supplier_id']
                                                   in (first, second)]):
                    not_split.append({'supplier_ids': [first, second], 'code': 'NOT_CHEAPER_THAN_SINGLE'})
                    continue
                splits.append(plan)
    by_total = lambda plan: (plan['status'] == 'INVALID', Decimal(plan['total_amount'] or 'Infinity'))
    shown = keep + [p for p in sorted(switches, key=by_total) if p['status'] != 'INVALID'][:MAX_PLAN_OPTIONS]         + [p for p in sorted(splits, key=by_total) if p['status'] != 'INVALID'][:MAX_PLAN_OPTIONS]
    infeasible = [{'plan_type': p['plan_type'], 'supplier_ids': [a['supplier_id'] for a in p['allocations']],
                   'violations': [v['code'] for v in p['violations']]} for p in switches + splits if p['status'] == 'INVALID']
    return {'status': 'OK', 'quantity': _amount_text(quantity), 'unit': case['unit'],
            'eligible': [{'supplier_id': sid, 'is_incumbent': suppliers[sid]['is_incumbent'],
                          'order_limits': {k: _won(v) for k, v in order_limits(suppliers[sid]['record'], case['unit']).items()},
                          'conditions': suppliers[sid]['conditions']} for sid in eligible],
            'excluded': [{'supplier_id': sid, 'reasons': s['reasons']} for sid, s in suppliers.items() if not s['eligible']],
            'plans': shown,
            'infeasible': infeasible + not_split,
            'search_scope': {'suppliers': len(eligible), 'split_pairs': pairs,
                             'split_allowed': policy.get('allow_split') is not False}} | (
        {} if any(p['status'] != 'INVALID' for p in shown) else
        {'next': '가능한 안이 없다. 다른 도구를 부르지 말고 HOLD로 최종 응답한다. reason에 빠진 업체의 이유(excluded)를 쓴다.'})


@register
@tool
def build_plan_candidates(runtime: ToolRuntime[RunContext], met_requirements: list[MetRequirement] | None = None) -> dict:
    '''업무의 최신 견적으로 구매안 후보를 만든다: 기존 업체 유지(KEEP), 전량 전환(SWITCH), 2곳 분산(SPLIT, 조건 안에서 가장 싼 배분).
    met_requirements: check_comparability의 requirements 중 state가 CHECK인 항목에서, 원문을 보고 충족이라고 판단한 것
      [{supplier_id, requirement}]. 넣지 않은 CHECK 항목은 불명확으로 보고 그 업체를 뺀다.
    필수 조건 미충족·불명확, 필수 누락, 미확인 비용이 남은 업체는 뺀다(excluded). 기존 업체는 예외로 유지안을 남기고
    그 사유를 conditions에 단다.

    반환 status:
    - OK: plans(plan_type·allocations(supplier_id·quote_version_id·quantity)·total_amount·status(VALID·CONDITIONAL)·
      conditions·tax_basis), eligible(order_limits: moq·multiple·max), excluded(reasons), infeasible(만들지 못한 안과 이유),
      search_scope(살펴본 업체·분산 조합 수)
    - CONDITIONS_MISSING / REVISION_CHANGED / CASE_NOT_FOUND
    '''
    return plan_candidates(_priority_store, _run_context(_priority_store, runtime), met_requirements or [])


def validate_plan(store: ComparisonStore, context: RunContext, plan_type: str, allocations: list[PlanAllocationInput],
                  mode: str, met: list[MetRequirement]) -> dict:
    """validate_purchase_plan의 본체. 분산안은 mode=quantities면 가장 가까운 가능한 배분으로 맞추고(조정 내역을 남긴다),
    mode=cheapest면 조건 안에서 가장 싼 배분을 찾는다."""
    case, suppliers = _plan_suppliers(store, context, met)
    if case is None:
        return suppliers
    ids = [a.supplier_id for a in allocations]
    unknown = [sid for sid in ids if sid not in suppliers]
    if unknown:
        return {'status': 'REJECTED', 'code': 'NOT_IN_COMPARISON', 'supplier_ids': unknown}
    shape = {'KEEP': len(ids) == 1 and suppliers[ids[0]]['is_incumbent'],
             'SWITCH': len(ids) == 1 and not suppliers[ids[0]]['is_incumbent'],
             'SPLIT': len(set(ids)) == 2 and len(ids) == 2}
    if not shape.get(plan_type):
        return {'status': 'REJECTED', 'code': 'PLAN_SHAPE',
                'hint': 'KEEP은 기존 업체 하나, SWITCH는 다른 업체 하나, SPLIT은 서로 다른 업체 둘이다.'}
    quantity, adjustments = _amount(case['quantity']), []
    if plan_type != 'SPLIT':
        parts = [(ids[0], quantity)]
    else:
        first, second = ((suppliers[sid]['quote_id'], suppliers[sid]['record']) for sid in ids)
        if mode == 'cheapest':
            found = cheapest_split(first, second, quantity, case['unit'])
            split = found['quantities'] if found['status'] == 'FOUND' else None
        else:
            wanted = _amount(allocations[0].quantity)
            if wanted is None:
                return {'status': 'REJECTED', 'code': 'QUANTITY_MISSING', 'hint': 'mode=quantities면 첫 업체 배정량을 넣는다.'}
            found = adjust_split(first, second, wanted, quantity, case['unit'])
            split = found['quantities']
            if found['status'] == 'ADJUSTED':
                adjustments.append(f"제안 {ids[0]} {_amount_text(wanted)}·{ids[1]} {_amount_text(quantity - wanted)} → "
                                   f"조정 {ids[0]} {_amount_text(split[0])}·{ids[1]} {_amount_text(split[1])} "
                                   f"(MOQ·발주 배수·공급량)")
        if split is None:
            return {'status': 'INVALID', 'plan_type': plan_type, 'violations': [{'code': found['status'], 'supplier_id': None}],
                    'hint': '이 두 업체로는 조건(MOQ·발주 배수·공급량)을 만족하는 배분이 없다. 다른 조합을 고르거나 이 안을 뺀다.'}
        parts = [(ids[0], split[0]), (ids[1], split[1])]
    result = _evaluate_plan(case, suppliers, plan_type, parts)
    if plan_type == 'SPLIT' and mode == 'cheapest' and result['status'] != 'INVALID':
        singles = [_evaluate_plan(case, suppliers, 'KEEP' if suppliers[sid]['is_incumbent'] else 'SWITCH', [(sid, quantity)])
                   for sid in ids]
        if not _cheaper_than_single(result, singles):
            return result | {'status': 'INVALID', 'violations': [{'code': 'NOT_CHEAPER_THAN_SINGLE', 'supplier_id': None}],
                             'hint': '한 업체가 전량을 맡는 안이 더 싸다. 분산할 비용 외 이유가 있으면 mode=quantities로 수량을 정한다.'}
    return result | {'adjustments': adjustments}


@register
@tool
def validate_purchase_plan(plan_type: Literal['KEEP', 'SWITCH', 'SPLIT'], allocations: list[PlanAllocationInput],
                           runtime: ToolRuntime[RunContext], mode: Literal['quantities', 'cheapest'] = 'cheapest',
                           met_requirements: list[MetRequirement] | None = None) -> dict:
    '''구매안 하나를 계산·검증한다. 담당자에게 보일 안은 모두 이 도구로 확인한다.
    plan_type: KEEP(기존 업체 전량) / SWITCH(다른 업체 하나 전량) / SPLIT(서로 다른 업체 둘)
    allocations: [{supplier_id, quantity}]. KEEP·SWITCH는 quantity를 비운다(업무 수량 전부).
    mode(SPLIT만): cheapest면 조건 안에서 가장 싼 배분을 찾는다. quantities면 첫 업체 quantity를 비용 외 이유로 정한 값으로
      넣는다(예: 처음 거래하는 업체 비중 제한). 도구가 MOQ·발주 배수·공급량에 맞는 가장 가까운 배분으로 맞추고 adjustments에 남긴다.
    met_requirements: build_plan_candidates에 넣은 것과 같은 목록.

    반환: status VALID·CONDITIONAL(기존 업체의 미확인이 남음, conditions)·INVALID(violations), plan_type, allocations
      (supplier_id·quote_version_id·quantity), total_amount, conditions, adjustments / REJECTED(code·hint)
    '''
    return validate_plan(_priority_store, _run_context(_priority_store, runtime), plan_type, allocations, mode,
                         met_requirements or [])


def revalidate_plans(store: ComparisonStore, case_id: str, plans: list[dict]) -> dict:
    """구매안 검증 노드: 안마다 최신 견적으로 총액·위반을 다시 계산한다(07 "코드 계산·조건 검증").
    반환: {check: PASS·FIX·NEED_INFO, violations: [{plan, code, supplier_id, expected, actual}]}
    견적이 최신이 아니면 NEED_INFO(③부터 다시), 위반·총액 차이가 있으면 FIX(④가 고친다).
    기존 업체의 총액을 계산할 수 있는데 유지안이 없으면 KEEP_MISSING으로 FIX한다(검토 카드의 비교 기준)."""
    case = _read(lambda: store.get_work_case(case_id))
    latest = set(case.get('latest_quote_version_ids') or [])
    records = dict(_read(lambda: store.get_quote_versions(case_id, sorted(latest))))
    violations, need_info = [], False
    for index, plan in enumerate(plans):
        allocations = plan['allocations']
        if any(a['quote_version_id'] not in latest for a in allocations):
            need_info = True
            violations.append({'plan': index, 'code': 'QUOTE_NOT_LATEST', 'supplier_id': None})
            continue
        total = plan_total([(a['quote_version_id'], records[a['quote_version_id']], _amount(a['quantity']))
                            for a in allocations], case['quantity'], case['unit'], case.get('policy') or {})
        single = len(allocations) == 1
        violations += [{'plan': index, **v} for v in total['violations']
                       if not (single and v['code'] == 'TAX_BASIS_UNKNOWN')]
        expected = _won(total['known_total'])
        if expected != (_amount_text(_amount(plan.get('total_amount'))) if plan.get('total_amount') else None):
            violations.append({'plan': index, 'code': 'TOTAL_MISMATCH', 'supplier_id': None,
                               'expected': expected, 'actual': plan.get('total_amount')})
    violations += _missing_keep(store, case_id, case, records, plans)
    return {'check': 'NEED_INFO' if need_info else 'FIX' if violations else 'PASS', 'violations': violations}


def _missing_keep(store: ComparisonStore, case_id: str, case: dict, records: dict, plans: list[dict]) -> list[dict]:
    """유지안이 빠졌는지. 기존 업체의 총액을 계산할 수 있으면 build_plan_candidates가 유지안을 만드므로 카드에 있어야 한다."""
    if any(plan['plan_type'] == 'KEEP' for plan in plans):
        return []
    incumbents = {mail['supplier_id'] for mail in _read(lambda: store.list_case_mails(case_id))
                  if mail.get('message_role') == 'INITIAL' and mail.get('supplier_id')}
    return [{'plan': None, 'code': 'KEEP_MISSING', 'supplier_id': record.supplier_id,
             'hint': '기존 업체 유지안(KEEP)을 validate_purchase_plan으로 확인해 plans에 넣는다.'}
            for quote_id, record in records.items()
            if record.supplier_id in incumbents and not cost_sheet(quote_id, record, case['quantity'], case['unit']).blocked]



# ─────────────────────────────────────────────
# ⑤ COUNTER_OFFER: calculate_counter_offer, validate_counter_offer, create_counter_offer_draft (+ 가격 검증 노드)
#   설계: docs/AGENT5_COUNTER_OFFER_DESIGN.md. 회사 정책(policy_jsonb)은 허용 인상률(max_increase_rate)과
#   기존 업체 선호 폭(incumbent_premium_rate) 두 값이다(4%·4%).
#   요청 인상률이 허용 인상률 이하이거나 요청 단가로도 대안보다 싸면 수용을 추천한다(역제안 생략).
#   그 외에는 [인상 전 단가, min(요청 단가, 인상 전 × (1+허용 인상률))] 안에서 역제안한다. 대안과 경쟁할 수 있는 선
#   (유지 상한 × (1+선호 폭))이 그 범위 안이면 상한을 그 선으로 낮추고, 범위보다 낮으면 그대로 두되 대안보다 비싸다고 알린다.
#   정책이 없으면 목표를 만들지 않는다(07 ⑤ "정책 없는 합리적 인상률 생성 금지"). 메일에는 다른 공급처 이야기를 넣지 않는다.
# ─────────────────────────────────────────────

COUNTER_STATUSES = ('PROPOSABLE', 'NO_POLICY', 'NOT_NEEDED', 'WITHIN_POLICY', 'NOT_COMPUTABLE', 'COSTS_UNKNOWN',
                    'PREVIOUS_UNKNOWN')
COUNTER_SKIP_STATUSES = ('NOT_NEEDED', 'WITHIN_POLICY')          # 역제안 없이 인상 수용을 추천하는 상태


class CounterOfferStore(PriorityStore, Protocol):
    """⑤ 도구의 저장소. 인상 전 단가·사유는 ①이 인상 통보 메일의 normalized_jsonb에 저장한다."""

    def get_increase_terms(self, case_id: str, supplier_id: str) -> dict | None:
        """이 업무에서 그 업체가 보낸 메일의 normalized_jsonb.fields 중 previous_unit_price·increase_reason
        {previous_unit_price, increase_reason, mail_id}. 없으면 None."""


class AlternativeAllocation(StrictModel):
    supplier_id: str
    quote_version_id: str
    quantity: str                      # 구매안 검증을 통과한 ④ 안의 배정량 그대로


def _counter_policy(case: dict) -> dict | None:
    """회사 정책(policy_jsonb)의 {max_increase_rate, incumbent_premium_rate}. 허용 인상률이 없거나 음수면 None이다.
    선호 폭이 없으면 0(대안보다 비싸면 유지하지 않음)으로 본다."""
    policy = case.get('policy') or {}
    rate, premium = _amount(policy.get('max_increase_rate')), _amount(policy.get('incumbent_premium_rate') or '0')
    if rate is None or rate < 0 or premium is None or premium < 0:
        return None
    return {'max_increase_rate': rate, 'incumbent_premium_rate': premium}


def counter_offer_numbers(store: CounterOfferStore, context: RunContext, alternative: list[AlternativeAllocation]) -> dict:
    """calculate_counter_offer의 본체. 기존 업체 요청·인상 전 단가, 대안 총액, 유지 상한 단가, 정책 범위, 허용 목표 범위.
    status: PROPOSABLE(허용 범위 있음) / NO_POLICY / NOT_NEEDED(요청 단가로도 대안보다 싸거나 같음) /
    WITHIN_POLICY(요청 인상률이 허용 인상률 이하) / NOT_COMPUTABLE(총액·세금 기준 비교 불가) /
    COSTS_UNKNOWN(기존 업체의 단가 외 비용 미확인) / PREVIOUS_UNKNOWN.
    NOT_NEEDED·WITHIN_POLICY는 수용 추천(skip_allowed). PROPOSABLE에서 above_alternative가 true면
    허용 범위의 어떤 값도 대안과 경쟁할 수 있는 선보다 비싸다."""
    case, stop = _priority_case(store, context)
    if stop:
        return stop
    incumbents = {mail['supplier_id'] for mail in _read(lambda: store.list_case_mails(context.case_id))
                  if mail.get('message_role') == 'INITIAL' and mail.get('supplier_id')}
    latest = dict(_read(lambda: store.get_quote_versions(context.case_id, case.get('latest_quote_version_ids') or [])))
    own = [(qid, record) for qid, record in latest.items() if record.supplier_id in incumbents]
    if not own:
        return {'status': 'REJECTED', 'code': 'NO_INCUMBENT_QUOTE', 'hint': '인상을 요청한 업체의 최신 견적이 없다. HOLD한다.'}
    quote_id, record = own[0]
    if not alternative or {a.supplier_id for a in alternative} <= incumbents \
            or any(a.quote_version_id not in latest for a in alternative):
        return {'status': 'REJECTED', 'code': 'ALTERNATIVE_INVALID',
                'hint': '대안은 구매안 검증을 통과한 비유지 안(SWITCH·SPLIT)의 배정량을 최신 견적 그대로 보낸다.'}
    plan = plan_total([(a.quote_version_id, latest[a.quote_version_id], _amount(a.quantity)) for a in alternative],
                      case['quantity'], case['unit'], case.get('policy') or {})
    sheet = cost_sheet(quote_id, record, case['quantity'], case['unit'])
    terms = _read(lambda: store.get_increase_terms(context.case_id, record.supplier_id)) or {}
    previous = _amount(terms.get('previous_unit_price'))
    result = {'supplier_id': record.supplier_id, 'quote_version_id': quote_id, 'unit': _unit_key(record.unit),
              'requested_unit_price': _won(sheet.unit_price), 'previous_unit_price': _won(previous),
              'previous_source': terms.get('mail_id'), 'increase_reason': terms.get('increase_reason'),
              'alternative': [a.model_dump() for a in alternative],
              'alternative_total': _won(plan['known_total']), 'alternative_violations': plan['violations'],
              'allowed_range': []}
    bases = {s.tax_basis for s in plan['sheets']}
    other_unknown = [c for c in plan['unknown_costs'] if c['supplier_id'] not in incumbents]
    if plan['known_total'] is None or plan['violations'] or other_unknown or len(bases) != 1:
        return {'status': 'OK', 'counter_status': 'NOT_COMPUTABLE', 'skip_allowed': False} | result
    ceiling = keep_price_ceiling(sheet, record, plan['known_total'], bases.pop())
    if ceiling['status'] not in ('COMPUTED', 'CONDITIONAL'):
        return {'status': 'OK', 'counter_status': 'NOT_COMPUTABLE', 'skip_allowed': False, 'ceiling': ceiling} | result
    if ceiling['status'] == 'CONDITIONAL':
        return {'status': 'OK', 'counter_status': 'COSTS_UNKNOWN', 'skip_allowed': False,
                'incumbent_unknown_costs': ceiling['unknown_costs']} | result
    top = Decimal(ceiling['ceiling_unit_price'])
    result |= {'ceiling_unit_price': ceiling['ceiling_unit_price'], 'ceiling_formula': ceiling['formula'],
               'incumbent_unknown_costs': ceiling['unknown_costs']}
    policy = _counter_policy(case)
    if sheet.unit_price is not None and sheet.unit_price <= top:
        status = 'NOT_NEEDED'
    elif policy is None:
        status = 'NO_POLICY'
    elif previous is None:
        status = 'PREVIOUS_UNKNOWN'
    elif sheet.unit_price <= previous * (1 + policy['max_increase_rate']):
        status = 'WITHIN_POLICY'
    else:
        status = 'PROPOSABLE'
        low, high = previous, min(sheet.unit_price, previous * (1 + policy['max_increase_rate']))
        competitive = top * (1 + policy['incumbent_premium_rate'])
        if competitive >= low:
            high = min(high, competitive)
        result |= {'allowed_range': [_amount_text(low), _amount_text(high)],
                   'competitive_unit_price': _amount_text(competitive), 'above_alternative': competitive < low}
    return {'status': 'OK', 'counter_status': status, 'skip_allowed': status in COUNTER_SKIP_STATUSES} | result


@register
@tool
def calculate_counter_offer(alternative: list[AlternativeAllocation], runtime: ToolRuntime[RunContext]) -> dict:
    '''기존 업체(인상 요청 업체)에 역제안할 수 있는 단가 범위를 계산한다.
    alternative: 비교할 대안의 배정량 [{supplier_id, quote_version_id, quantity}]. input의 validated_plans 중
      가장 싼 비유지 안(VALID)을 그대로 보낸다.

    반환 status OK: counter_status(PROPOSABLE: allowed_range 안에서 목표를 정한다 / NO_POLICY: 회사 정책이 없어 목표를
      만들지 않는다 / NOT_NEEDED: 요청 단가로도 대안보다 싸다 / WITHIN_POLICY: 요청 인상률이 회사 허용 인상률 이하다 /
      NOT_COMPUTABLE / COSTS_UNKNOWN: 기존 업체의 단가 외 비용(incumbent_unknown_costs)을 모른다 /
      PREVIOUS_UNKNOWN: 인상 전 단가 근거가 없다), skip_allowed(true면 역제안 없이 인상 수용을 추천한다),
      requested_unit_price, previous_unit_price(previous_source 메일), ceiling_unit_price(이 단가 이하면 대안보다 싸다),
      alternative_total, allowed_range([최소, 최대]), above_alternative(true면 범위 안의 목표가 받아들여져도
      대안보다 비싸다), unit, increase_reason
    / REJECTED(code·hint) / CONDITIONS_MISSING / REVISION_CHANGED / CASE_NOT_FOUND
    '''
    return counter_offer_numbers(_priority_store, _run_context(_priority_store, runtime), alternative)


def check_counter_target(store: CounterOfferStore, context: RunContext, target_unit_price: str,
                         alternative: list[AlternativeAllocation]) -> dict:
    """validate_counter_offer의 본체. 목표 단가가 허용 범위(정책·유지 상한) 안인지 확인한다."""
    numbers = counter_offer_numbers(store, context, alternative)
    if numbers.get('status') != 'OK':
        return numbers
    target = _amount(target_unit_price)
    violations = []
    if numbers['counter_status'] != 'PROPOSABLE':
        violations.append({'code': numbers['counter_status']})
    elif target is None or not Decimal(numbers['allowed_range'][0]) <= target <= Decimal(numbers['allowed_range'][1]):
        violations.append({'code': 'OUT_OF_RANGE', 'allowed_range': numbers['allowed_range']})
    return {'status': 'INVALID' if violations else 'VALID', 'target_unit_price': _won(target),
            'violations': violations, 'numbers': numbers}


@register
@tool
def validate_counter_offer(target_unit_price: str, alternative: list[AlternativeAllocation],
                           runtime: ToolRuntime[RunContext]) -> dict:
    '''목표 단가가 회사 정책과 유지 상한을 만족하는지 검증한다. alternative는 calculate_counter_offer에 보낸 것과 같다.
    반환: status VALID / INVALID(violations: OUT_OF_RANGE(allowed_range)·NO_POLICY 등), target_unit_price, numbers
    '''
    return check_counter_target(_priority_store, _run_context(_priority_store, runtime), target_unit_price, alternative)


COUNTER_SUBJECT = '{보내는회사}{품목} 단가 관련 회신드립니다'
COUNTER_BODY = ('안녕하세요. {업체명} 담당자님\n'
                '{회사소개}\n\n'
                '보내 주신 {품목} 단가 조정 안내 잘 받았습니다. 검토 결과 아래 조건으로 거래를 이어가기를 요청드립니다.\n\n'
                '<요청 조건>\n'
                '- 품목: {품목}\n'
                '- 단가: {목표단가}원/{단위}\n'
                '{조건목록}\n\n'
                '{회신기한}에 회신 부탁드립니다.\n'
                '감사합니다.\n'
                '{서명}')


def save_counter_offer_draft(store: CounterOfferStore, context: RunContext, target_unit_price: str, conditions: list[str],
                             alternative: list[AlternativeAllocation]) -> dict:
    """create_counter_offer_draft의 본체. 목표를 다시 검증하고, 통과할 때만 기존 업체에 보낼 초안을 저장한다."""
    checked = check_counter_target(store, context, target_unit_price, alternative)
    if checked.get('status') != 'VALID':
        return {'status': 'REJECTED', 'code': 'NOT_VALIDATED', 'validation': checked}
    numbers = checked['numbers']
    contact = _read(lambda: store.get_supplier(context.case_id, numbers['supplier_id'])) or {}
    recipient = (contact.get('contact_email') or '').strip()
    if not recipient:
        return {'status': 'REJECTED', 'code': 'NO_CONTACT'}
    case = (_read(lambda: store.get_work_case(context.case_id)) or {}) | {'id': context.case_id}
    style = _read(lambda: store.current_send_settings(context.case_id)) or {}
    lines = '\n'.join(f'- {text}' for text in (mail_value(c, MAIL_VALUE_LIMITS['조건']) for c in conditions) if text)
    subject, body = _mail_text(COUNTER_SUBJECT, COUNTER_BODY, {
        '회신기한': REPLY_DEADLINE, '품목': recipient_item_name(store, case, numbers['supplier_id']),
        '업체명': contact.get('name') or '담당자', '목표단가': f"{Decimal(checked['target_unit_price']):,}",
        '단위': numbers['unit'] or '', '조건목록': lines, **_style_values(style)})
    key = hashlib.sha256(f"{context.case_id}|{context.input_revision}|COUNTER_OFFER|{numbers['supplier_id']}|"
                         f"{checked['target_unit_price']}".encode()).hexdigest()
    record = MailDraftRecord(
        work_case_id=context.case_id, supplier_id=numbers['supplier_id'], purpose='COUNTER_OFFER', recipient=recipient,
        subject=subject, body_text=body,
        evidence_refs_jsonb=[f"quote_versions:{numbers['quote_version_id']}"] + (
            [f"mail_messages:{numbers['previous_source']}.previous_unit_price"] if numbers.get('previous_source') else []) + (
            [f'agent_runs:{context.agent_run_id}'] if context.agent_run_id else []),
        input_revision=context.input_revision, style_snapshot_jsonb=style, idempotency_key=key)
    saved = store.insert_mail_draft(record, context.input_revision)
    if saved is None:
        return {'status': 'REVISION_CHANGED'}
    return {'status': 'DRAFTED', 'draft_id': saved[0], 'target_unit_price': checked['target_unit_price'],
            'supplier_id': numbers['supplier_id'], 'quote_version_id': numbers['quote_version_id'],
            'conditions': [text.strip() for text in conditions if text.strip()], 'alternative': numbers['alternative']}


@register
@tool
def create_counter_offer_draft(target_unit_price: str, alternative: list[AlternativeAllocation],
                               runtime: ToolRuntime[RunContext], conditions: list[str] | None = None) -> dict:
    '''기존 업체에 보낼 역제안 메일 초안을 저장한다(발송하지 않는다). 목표 단가를 다시 검증하고 통과할 때만 만든다.
    메일에는 목표 단가와 conditions(예: "12월 물량 전량 발주 시")만 들어가고, 다른 공급처 이야기는 넣지 않는다.
    반환: DRAFTED(draft_id·target_unit_price·supplier_id) / REJECTED(code: NOT_VALIDATED·NO_CONTACT) / REVISION_CHANGED
    '''
    return save_counter_offer_draft(_priority_store, _run_context(_priority_store, runtime), target_unit_price,
                                    conditions or [], alternative)


def _is_validated_alternative(alternative: list[AlternativeAllocation], plans: list[dict]) -> bool:
    """역제안 대안이 구매안 검증을 통과한 비유지 안(VALID인 SWITCH·SPLIT)의 배정과 같은지. 업체·견적·수량으로 비교한다."""
    def key(allocations):
        try:
            return sorted((str(a['supplier_id']), str(a['quote_version_id']), Decimal(str(a['quantity'])).normalize())
                          for a in allocations)
        except (KeyError, TypeError, ArithmeticError, ValueError):
            return None
    chosen = key([a.model_dump() for a in alternative])
    return chosen is not None and bool(chosen) and any(
        key(p.get('allocations') or []) == chosen for p in plans
        if p.get('plan_type') != 'KEEP' and p.get('validation_status') == 'VALID')


def revalidate_counter_offer(store: CounterOfferStore, case_id: str, input_revision: int, target: dict | None,
                             skipped: bool, plans: list[dict]) -> dict:
    """가격 검증 노드: 역제안 목표(또는 생략)를 최신 견적·정책으로 다시 확인한다.
    반환: {check: PASS·FIX·NEED_INFO, reason}. 목표가 범위 밖이거나 허용되지 않는 생략이면 FIX(⑤가 고친다),
    대안 견적이 최신이 아니면 NEED_INFO."""
    context = RunContext(case_id, input_revision)
    if target is not None:
        alternative = [AlternativeAllocation(**a) for a in target.get('alternative', [])]
    else:
        cheapest = min((p for p in plans if p['plan_type'] != 'KEEP' and p.get('validation_status') == 'VALID'),
                       key=lambda p: Decimal(p['total_amount']), default=None)
        alternative = [AlternativeAllocation(supplier_id=a['supplier_id'], quote_version_id=a['quote_version_id'],
                                             quantity=a['quantity']) for a in (cheapest or {}).get('allocations', [])]
    numbers = counter_offer_numbers(store, context, alternative)
    if numbers.get('status') != 'OK':
        return {'check': 'NEED_INFO', 'reason': numbers.get('code') or numbers.get('status')}
    if skipped:
        return {'check': 'PASS' if numbers['skip_allowed'] else 'FIX',
                'reason': None if numbers['skip_allowed'] else f"생략이 허용되지 않는다({numbers['counter_status']})"}
    # 견적이 최신인지(NEED_INFO) 먼저 본 뒤, 대안이 검증된 구매안인지 본다
    if not _is_validated_alternative(alternative, plans):
        return {'check': 'FIX', 'reason': '비교한 대안이 검증된 구매안(VALID인 전환·분산안)의 배정과 다르다. '
                                         'validated_plans 중 하나의 allocations를 그대로 쓴다.'}
    checked = check_counter_target(store, context, target['target_unit_price'], alternative)
    return {'check': 'PASS' if checked['status'] == 'VALID' else 'FIX',
            'reason': None if checked['status'] == 'VALID' else json.dumps(checked['violations'], ensure_ascii=False)}
