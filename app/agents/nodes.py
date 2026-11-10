"""에이전트가 아닌 그래프 노드 (07 2·4절). 결정 가능한 검증은 Python·룰로 하고 LLM 검증 에이전트로 부르지 않는다.

각 노드는 CaseState를 받아 바꿀 필드만 dict로 돌려준다. graph.build_graph(nodes=Nodes(...))로 교체·주입한다.
구매안·가격 검증은 app.agents.tools의 공통 계산(plan_total 등)을 에이전트 도구와 함께 쓴다(같은 숫자가 나온다).
문서 읽기는 app.features.review.infrastructure.documents를 재사용한다(텍스트 PDF·텍스트·HTML·xlsx).
전처리에서는 OCR하지 않는다. 스캔 PDF의 빈 쪽은 에이전트가 그 쪽을 읽을 때 저장소(read_mail_part)가
documents.ocr_pdf_page로 읽는다(저장소 연결은 서버 저장소 작업에서 한다).
"""
import io
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol

from app.agents import tools
from app.features.review.infrastructure import documents
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


class DocumentStore(Protocol):
    """전처리 노드의 저장소. 첨부 본체는 메일 제공자(Gmail)에서 받고, 읽은 결과는 mail_messages.normalized_jsonb에 둔다."""

    def list_unread_attachments(self, case_id: str, mail_ids: list[str]) -> list[dict]:
        """업무에 속한 메일 중 아직 읽은 결과가 없는 첨부 {mail_id, attachment_ref, filename, content_type, size_bytes}."""

    def download_attachment(self, case_id: str, mail_id: str, attachment_ref: str) -> bytes | None:
        """첨부 본체. 소유·업무 범위를 확인하고 받지 못하면 None."""

    def save_document(self, case_id: str, mail_id: str, attachment_ref: str, document: dict) -> None:
        """첨부를 읽은 결과 {status, pages}를 normalized_jsonb.documents[attachment_ref]에 저장한다.
        read_mail_part가 이 pages를 쪽 단위로 돌려준다."""


_document_store: DocumentStore | None = None


def use_document_store(store: DocumentStore | None) -> None:
    global _document_store
    _document_store = store


MAX_ATTACHMENT_BYTES = 20 * 1024 * 1024
MAX_SHEET_ROWS, MAX_SHEET_COLUMNS = 2000, 50
XLSX = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
_EXTENSION_TYPES = {'.pdf': 'application/pdf', '.txt': 'text/plain', '.csv': 'text/csv', '.html': 'text/html',
                    '.htm': 'text/html', '.xlsx': XLSX}
# 읽지 못한 첨부를 담당자에게 알리는 문장 (status → 사유)
DOCUMENT_ISSUES = {'NO_TEXT': '글자가 없는 PDF(스캔본)라 읽지 못했습니다', 'UNSUPPORTED': '지원하지 않는 첨부 형식입니다',
                   'TOO_LARGE': '첨부가 너무 커서 읽지 않았습니다', 'FAILED': '첨부를 읽는 중 오류가 났습니다',
                   'DOWNLOAD_FAILED': '첨부를 받아오지 못했습니다',
                   'PDF_CONTENT': 'PDF가 손상됐거나 암호·쪽수 한도 때문에 읽을 수 없습니다'}
PDF_RETRY_EXHAUSTED = '첨부를 읽는 중 오류가 나 {attempts}번 시도했지만 읽지 못했습니다'


def _decode(content: bytes) -> str:
    """텍스트 첨부를 UTF-8(BOM 포함), 안 되면 CP949로 읽는다."""
    try:
        return content.decode('utf-8-sig')
    except UnicodeDecodeError:
        return content.decode('cp949')


def _sheet_pages(content: bytes) -> list[str]:
    """xlsx를 시트마다 한 쪽으로 읽는다. 값이 있는 셀만 'B3: 5500'처럼 셀 위치와 함께 행 단위로 적는다."""
    from openpyxl import load_workbook
    book = load_workbook(io.BytesIO(content), read_only=True, data_only=True)
    pages = []
    for sheet in book.worksheets:
        lines = [f'[{sheet.title}]']
        for row in sheet.iter_rows(max_row=MAX_SHEET_ROWS, max_col=MAX_SHEET_COLUMNS):
            cells = [f'{cell.coordinate}: {cell.value.isoformat() if hasattr(cell.value, "isoformat") else cell.value}'
                     for cell in row if cell.value is not None and str(cell.value).strip() != ''
                     and hasattr(cell, 'coordinate')]
            if cells:
                lines.append(' | '.join(cells))
        pages.append('\n'.join(lines))
    book.close()
    return pages


def read_document(filename: str, content_type: str | None, content: bytes) -> dict:
    """첨부 하나를 쪽 단위 텍스트로 읽는다. 반환: {status: READ·NO_TEXT·UNSUPPORTED·TOO_LARGE·FAILED, pages}.
    PDF는 쪽마다, xlsx는 시트마다 한 쪽이다. 여기서는 OCR하지 않는다. 글자가 없는 PDF(스캔본)는 NO_TEXT이고
    pages는 쪽수만큼의 ''다(빈 쪽의 OCR은 저장소 read_mail_part 몫이다).
    PDF 기술 오류는 documents.PDF_MAX_ATTEMPTS번까지 시도한다. 저장 형식이라 시도 기록은 넣지 않는다(_read_document)."""
    return _read_document(filename, content_type, content)[0]


def _read_document(filename: str, content_type: str | None, content: bytes) -> tuple[dict, dict | None]:
    """(read_document 결과, PDF 시도 기록 {attempts, error_kind, retry_exhausted} 또는 None)"""
    kind = (content_type or '').split(';')[0].strip().lower()
    if kind not in _EXTENSION_TYPES.values():
        kind = _EXTENSION_TYPES.get(Path(filename or '').suffix.lower(), kind)
    if len(content) > MAX_ATTACHMENT_BYTES:
        return {'status': 'TOO_LARGE', 'pages': []}, None
    if kind == 'application/pdf':
        result = documents.read_pdf_document(content)
        log = {key: result[key] for key in ('attempts', 'error_kind', 'retry_exhausted')}
        return {'status': result['status'], 'pages': result['pages']}, log
    return _read_other(kind, content), None


def _read_other(kind: str, content: bytes) -> dict:
    try:
        if kind in ('text/plain', 'text/csv'):
            return {'status': 'READ', 'pages': [_decode(content)]}
        if kind == 'text/html':
            parser = documents.HtmlText()
            parser.feed(_decode(content))
            return {'status': 'READ', 'pages': [''.join(parser.parts)]}
        if kind == XLSX:
            return {'status': 'READ', 'pages': _sheet_pages(content)}
    except Exception:
        return {'status': 'FAILED', 'pages': []}
    return {'status': 'UNSUPPORTED', 'pages': []}


def preprocess(state) -> dict:
    """이번 실행의 메일(state['mail_ids']) 첨부 중 아직 읽지 않은 것을 쪽 단위 텍스트로 읽어 저장한다.
    ①은 read_mail_evidence로 그 쪽을 읽는다. 첨부 하나를 읽지 못해도 실행은 계속하고 사유를 unresolved에 남긴다.
    받아오지 못한 첨부는 저장하지 않아 다음 실행에서 다시 시도한다.
    반환: {'unresolved': ['<mail_id> <파일명>: 사유', ...]} (모두 읽었으면 {})"""
    if _document_store is None:
        raise RuntimeError('document store is not configured')
    issues = []
    for item in _document_store.list_unread_attachments(state['case_id'], state.get('mail_ids', [])):
        content = _document_store.download_attachment(state['case_id'], item['mail_id'], item['attachment_ref'])
        if content is None:
            issues.append(f"{item['mail_id']} {item['filename']}: {DOCUMENT_ISSUES['DOWNLOAD_FAILED']}")
            continue
        document, log = _read_document(item['filename'], item.get('content_type'), content)
        _document_store.save_document(state['case_id'], item['mail_id'], item['attachment_ref'], document)
        if document['status'] != 'READ':
            issues.append(f"{item['mail_id']} {item['filename']}: {_document_issue(document['status'], log)}")
    return {'unresolved': issues} if issues else {}


def _document_issue(status: str, log: dict | None) -> str:
    """읽지 못한 사유. PDF는 내용 문제와 기술 오류(재시도 소진)를 나눠 적는다."""
    if status == 'FAILED' and log:
        if log['retry_exhausted']:
            return PDF_RETRY_EXHAUSTED.format(attempts=log['attempts'])
        if log['error_kind'] == 'CONTENT':
            return DOCUMENT_ISSUES['PDF_CONTENT']
    return DOCUMENT_ISSUES[status]


def plan_validation(state) -> dict:
    """④ 구매안을 최신 견적으로 다시 계산해 총액·공급량·MOQ·발주 배수·분산 정책·세금 기준·유지안 포함을 검증한다.
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
    state['last_action']이 COUNTER_OFFER_SKIPPED면 counter_offer는 None이다. 이때는 수용 추천 조건(요청 인상률 ≤
    허용 인상률, 또는 요청 단가로도 대안보다 쌈)을 다시 확인한다 (사유: state['counter_offer_skip_reason']).
    PASS → 검토 카드, FIX(정책 범위 밖 목표 조건·조건에 맞지 않는 생략) → ⑤, NEED_INFO(근거·정책 없음) → ③.
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
