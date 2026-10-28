from unittest.mock import MagicMock

import pytest

from app.features.review.infrastructure.documents import document_text
from app.core.errors import ReviewRequired


def pdf_source():
    return {'mail_body': '원문 본문', 'attachments': [{'storage_path': 'W-20261026-001/attach_001.pdf',
            'content_type': 'application/pdf', 'size_bytes': 3, 'sha256': 'a' * 64, 'original_name': '첨부.pdf'}]}


def test_pdf_has_exactly_four_technical_attempts(monkeypatch):
    calls = []
    def broken(content):
        calls.append(content)
        raise ValueError('unreadable')
    monkeypatch.setattr('app.features.review.infrastructure.documents.read_pdf', broken)
    storage = MagicMock()
    storage.download_verified.return_value = b'pdf'
    with pytest.raises(ReviewRequired) as error:
        document_text('W-20261026-001', pdf_source(), storage, 10000)
    assert len(calls) == 4 and error.value.issue['code'] == 'PDF_READ_FAILED'


def test_empty_image_pdf_requires_ocr_without_fabricated_text(monkeypatch):
    monkeypatch.setattr('app.features.review.infrastructure.documents.read_pdf', lambda content: '')
    with pytest.raises(ReviewRequired) as error:
        document_text('W-20261026-001', pdf_source(), MagicMock(), 10000)
    assert error.value.issue['code'] == 'OCR_REQUIRED'


def test_oversized_document_is_not_silently_truncated():
    with pytest.raises(ReviewRequired) as error:
        document_text('W-20261026-001', {'mail_body': 'a' * 10000, 'attachments': []}, MagicMock(), 1000)
    assert error.value.issue['code'] == 'DOCUMENT_TOO_LARGE'


def test_relocated_pdf_worker_still_runs_in_project_root():
    from io import BytesIO
    from pypdf import PdfWriter
    from app.features.review.infrastructure.documents import read_pdf
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    data = BytesIO()
    writer.write(data)
    assert read_pdf(data.getvalue()) == ''
