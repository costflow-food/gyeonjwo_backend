"""스캔 PDF OCR·PDF 기술 오류 재시도 테스트. 샘플은 tests/make_ocr_fixtures.py가 만든 합성 PDF와 strawberry 공문이다.

- 재시도·상태 판단 테스트는 PDF 워커를 가짜로 바꿔 OCR 모델 없이 돈다.
- 실제 OCR 테스트(@needs_ocr)는 RapidOCR와 모델 파일이 없으면 건너뛴다.
  모델 준비: python -m app.features.review.infrastructure.pdf_worker setup models/ocr
"""
import importlib.util
from pathlib import Path

import pytest

from app.agents import nodes
from app.agents.schemas import RunContext
from app.agents.tools import QUOTE_FIELDS, EvidenceRef, _SourceCheck, load_evidence
from app.features.review.infrastructure import documents
from app.features.review.infrastructure.documents import (PDF_MAX_ATTEMPTS, OcrUnavailableError, PdfContentError,
                                                          PdfTechnicalError, ocr_pdf_page, read_pdf_document)
from app.features.review.infrastructure.pdf_worker import OCR_MODEL_FILES

FIXTURES = Path(__file__).parent / 'fixtures' / 'ocr'


def _pdf(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def _ocr_ready() -> bool:
    return importlib.util.find_spec('rapidocr') is not None and all(
        (Path(documents.ocr_model_dir()) / name).is_file() for name in OCR_MODEL_FILES)


needs_ocr = pytest.mark.skipif(not _ocr_ready(), reason='RapidOCR 모델이 준비되지 않았다 (pdf_worker setup)')


def _no_space(text: str) -> str:
    return ''.join(text.split())


# ── 텍스트 레이어 읽기 (실제 워커, OCR 모델 불필요) ──────────────────────────

def test_text_pdf_reads_as_before():
    result = read_pdf_document(_pdf('text_quote.pdf'))
    assert result['status'] == 'READ'
    assert len(result['pages']) == 1 and '54,500' in result['pages'][0]
    assert result['attempts'] == 1 and result['error_kind'] is None and result['retry_exhausted'] is False


def test_scan_pdf_keeps_page_count():
    """스캔본도 쪽수가 남아야 에이전트가 쪽을 골라 요청할 수 있다 (예전에는 빈 목록이라 0쪽)."""
    result = read_pdf_document(_pdf('scan_quote.pdf'))
    assert result['status'] == 'NO_TEXT'
    assert result['pages'] == ['', '']
    assert result['attempts'] == 1


def test_legacy_read_pdf_still_empty_for_scan():
    """구 review 파이프라인은 전체 텍스트가 비면 OCR_REQUIRED로 멈춘다. 그 판단이 바뀌지 않는다."""
    assert documents.read_pdf(_pdf('scan_quote.pdf')) == ''


def test_encrypted_pdf_is_content_error_without_retry():
    result = read_pdf_document(_pdf('encrypted.pdf'))
    assert result['status'] == 'FAILED'
    assert result['error_kind'] == 'CONTENT' and result['attempts'] == 1 and result['retry_exhausted'] is False


# ── 재시도 (가짜 워커) ─────────────────────────────────────────────────

class FlakyWorker:
    """처음 failures번은 error를 올리고 그 뒤에는 result를 돌려준다."""

    def __init__(self, failures: int, error: Exception, result: dict):
        self.failures, self.error, self.result, self.calls = failures, error, result, 0

    def __call__(self, content, args=(), timeout=None):
        self.calls += 1
        if self.calls <= self.failures:
            raise self.error
        return self.result


@pytest.mark.parametrize('failures, attempts', [(0, 1), (1, 2), (3, 4)])
def test_technical_error_is_retried_until_success(monkeypatch, failures, attempts):
    worker = FlakyWorker(failures, PdfTechnicalError('x'), {'text': '1페이지\n단가', 'pages': ['단가']})
    monkeypatch.setattr(documents, '_pdf_worker', worker)
    result = read_pdf_document(b'%PDF')
    assert result['status'] == 'READ' and result['attempts'] == attempts and worker.calls == attempts


def test_technical_error_stops_after_first_plus_three_retries(monkeypatch):
    worker = FlakyWorker(99, PdfTechnicalError('x'), {})
    monkeypatch.setattr(documents, '_pdf_worker', worker)
    result = read_pdf_document(b'%PDF')
    assert PDF_MAX_ATTEMPTS == 4
    assert worker.calls == 4
    assert result == {'status': 'FAILED', 'pages': [], 'attempts': 4, 'error_kind': 'TECHNICAL',
                      'retry_exhausted': True}


@pytest.mark.parametrize('error, kind', [(PdfContentError('x'), 'CONTENT'),
                                         (OcrUnavailableError('x'), 'OCR_UNAVAILABLE')])
def test_content_and_setup_errors_are_not_retried(monkeypatch, error, kind):
    worker = FlakyWorker(99, error, {})
    monkeypatch.setattr(documents, '_pdf_worker', worker)
    result = ocr_pdf_page(b'%PDF', 1)
    assert worker.calls == 1
    assert result['status'] == 'FAILED' and result['error_kind'] == kind and result['retry_exhausted'] is False


def test_ocr_retry_exhausted(monkeypatch):
    worker = FlakyWorker(99, PdfTechnicalError('x'), {})
    monkeypatch.setattr(documents, '_pdf_worker', worker)
    result = ocr_pdf_page(b'%PDF', 1)
    assert worker.calls == 4
    assert result == {'status': 'FAILED', 'text': '', 'text_source': 'OCR', 'attempts': 4,
                      'error_kind': 'TECHNICAL', 'retry_exhausted': True}


def test_ocr_blank_page_is_no_text_not_error(monkeypatch):
    monkeypatch.setattr(documents, '_pdf_worker', FlakyWorker(0, PdfTechnicalError('x'), {'text': ' \n'}))
    result = ocr_pdf_page(b'%PDF', 2)
    assert result['status'] == 'NO_TEXT' and result['error_kind'] is None and result['attempts'] == 1


def test_missing_models_is_ocr_unavailable(monkeypatch, tmp_path):
    """모델 파일이 없으면 '스캔본이라 글자 없음'이 아니라 설정 문제로 남고, 다시 시도하지 않는다 (실제 워커)."""
    monkeypatch.setenv('OCR_MODEL_DIR', str(tmp_path))
    result = ocr_pdf_page(_pdf('scan_quote.pdf'), 1)
    assert result['status'] == 'FAILED' and result['error_kind'] == 'OCR_UNAVAILABLE' and result['attempts'] == 1


# ── 실제 OCR (RapidOCR 모델 필요) ───────────────────────────────────────

@needs_ocr
def test_ocr_page_out_of_range_is_content_error():
    result = ocr_pdf_page(_pdf('scan_quote.pdf'), 3)
    assert result['error_kind'] == 'CONTENT' and result['attempts'] == 1


@needs_ocr
def test_ocr_reads_scanned_quote():
    result = ocr_pdf_page(_pdf('scan_quote.pdf'), 1)
    assert result['status'] == 'READ' and result['text_source'] == 'OCR' and result['attempts'] == 1
    text = _no_space(result['text'])
    assert '54,500원' in text and '10kg/박스' in text


@needs_ocr
def test_ocr_blank_scanned_page_has_no_text():
    assert ocr_pdf_page(_pdf('scan_quote.pdf'), 2)['status'] == 'NO_TEXT'


@needs_ocr
def test_ocr_reads_both_items():
    text = _no_space(ocr_pdf_page(_pdf('scan_two_items.pdf'), 1)['text'])
    assert '54,500원' in text and '38,000원' in text and '5kg/박스' in text


# 정답 값이 OCR 텍스트를 근거로 원문 대조(store_structured_mail의 _SourceCheck)를 통과하는지.
# strawberry 공문은 텍스트 PDF지만 쪽을 그림으로 렌더링해 OCR하므로 스캔본 대용으로 쓴다.
# 2026-11-05 엔진 비교: RapidOCR 한국어 v5 31/31, Tesseract 5.4 17/31
OCR_TRUTH = {
    'strawberry/V1/가격조정_공문_냉동딸기(한결냉동식품).pdf': [
        ('supplier_name', '한결냉동식품 주식회사'), ('item_name', '냉동딸기(IQF)'), ('specification', '특품 10kg/박스'),
        ('previous_unit_price', '5,000원'), ('unit_price', '5,500원'), ('effective_from', '2026. 11. 1.')],
    'strawberry/V2/가격조정_공문_냉동딸기_박스단가(동해프레시).pdf': [
        ('supplier_name', '동해프레시 주식회사'), ('item_name', '냉동딸기(IQF)'), ('specification', '특품 10kg 박스'),
        ('previous_unit_price', '50,000원'), ('unit_price', '55,000원'), ('effective_from', '2026. 11. 16.'),
        ('quantity_per_order_unit', '10kg')],
    'strawberry/V3/가격조정_요청_공문_냉동딸기(청솔농산).pdf': [
        ('supplier_name', '청솔농산 주식회사'), ('item_name', '냉동딸기(IQF)'), ('specification', '특품 10kg/박스'),
        ('previous_unit_price', '5,000원'), ('unit_price', '5,600원'), ('effective_from', '2026. 11. 1.')],
    'ocr/scan_quote.pdf': [
        ('item_name', '냉동 딸기 특품'), ('specification', '10kg/박스'), ('unit_price', '54,500원'), ('unit', '박스'),
        ('effective_from', '2026년 12월 1일'), ('valid_until', '2026년 12월 31일까지')],
    'ocr/scan_two_items.pdf': [
        ('item_name', '냉동 딸기 특품'), ('specification', '10kg/박스'), ('unit_price', '54,500원'),
        ('item_name', '냉동 블루베리'), ('specification', '5kg/박스'), ('unit_price', '38,000원')],
}


@needs_ocr
@pytest.mark.parametrize('name', list(OCR_TRUTH))
def test_ocr_text_supports_true_values(name):
    text = ocr_pdf_page((FIXTURES.parent / name).read_bytes(), 1)['text']

    class Store:
        def read_mail_part(self, case_id, mail_id, part, page):
            return {'text': text, 'location': 'att-1:page:1', 'text_source': 'OCR'}

    check = _SourceCheck(Store(), 'c-1')
    missed = [value for field, value in OCR_TRUTH[name]
              if check(EvidenceRef(mail_id='m-1', part='att-1', page=1), value,
                       numeric=QUOTE_FIELDS[field][0] in ('price', 'number'))[0] is not None]
    assert missed == []
    assert '1069' not in _no_space(text)        # Tesseract가 '10kg'를 읽던 그럴듯한 오인식


# ── 전처리 노드: 저장 형식과 사유 문구 ─────────────────────────────────────

def test_read_document_keeps_storage_shape():
    """review_graph_store·graph_store가 이 결과를 그대로 저장하므로 키는 status·pages뿐이다."""
    assert nodes.read_document('q.pdf', 'application/pdf', _pdf('scan_quote.pdf')) == {'status': 'NO_TEXT',
                                                                                        'pages': ['', '']}
    assert nodes.read_document('q.pdf', 'application/pdf', _pdf('text_quote.pdf'))['status'] == 'READ'


class DocumentStore:
    def __init__(self):
        self.saved = {}

    def list_unread_attachments(self, case_id, mail_ids):
        return [{'mail_id': 'm-1', 'attachment_ref': 'att-1', 'filename': 'quote.pdf',
                 'content_type': 'application/pdf'}]

    def download_attachment(self, case_id, mail_id, attachment_ref):
        return b'%PDF'

    def save_document(self, case_id, mail_id, attachment_ref, document):
        self.saved[(mail_id, attachment_ref)] = document


@pytest.mark.parametrize('error, issue', [
    (PdfTechnicalError('x'), '첨부를 읽는 중 오류가 나 4번 시도했지만 읽지 못했습니다'),
    (PdfContentError('x'), nodes.DOCUMENT_ISSUES['PDF_CONTENT']),
])
def test_preprocess_separates_retry_exhausted_from_content_error(monkeypatch, error, issue):
    store = DocumentStore()
    monkeypatch.setattr(documents, '_pdf_worker', FlakyWorker(99, error, {}))
    nodes.use_document_store(store)
    try:
        result = nodes.preprocess({'case_id': 'c-1', 'mail_ids': ['m-1']})
    finally:
        nodes.use_document_store(None)
    assert result == {'unresolved': [f'm-1 quote.pdf: {issue}']}
    assert store.saved[('m-1', 'att-1')] == {'status': 'FAILED', 'pages': []}


# ── read_mail_evidence 응답 (6단계) ─────────────────────────────────────

class PartStore:
    def __init__(self, part):
        self.part = part

    def read_mail_part(self, case_id, mail_id, part, page):
        return self.part


CONTEXT = RunContext(case_id='c-1', input_revision=1)


def test_evidence_marks_text_source_default_text():
    result = load_evidence(PartStore({'text': '단가 54,500원', 'location': 'body'}), CONTEXT, 'm-1', 'body', None)
    assert result['status'] == 'OK' and result['text_source'] == 'TEXT'


def test_evidence_marks_ocr_text():
    part = {'text': '단가 54,500원', 'location': 'att-1:page:1', 'text_source': 'OCR'}
    result = load_evidence(PartStore(part), CONTEXT, 'm-1', 'att-1', 1)
    assert result['status'] == 'OK' and result['text_source'] == 'OCR' and result['empty'] is False


def test_evidence_unreadable_page():
    part = {'text': '', 'location': 'att-1:page:1', 'text_source': 'OCR', 'unreadable': True, 'attempts': 4,
            'retry_exhausted': True}
    result = load_evidence(PartStore(part), CONTEXT, 'm-1', 'att-1', 1)
    assert result == {'status': 'UNREADABLE', 'mail_id': 'm-1', 'part': 'att-1', 'page': 1, 'attempts': 4,
                      'retry_exhausted': True}
