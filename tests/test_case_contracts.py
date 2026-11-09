"""KAN-161: API-03 progress error/waiting reason and API-25 can_download."""
from app.features.mail.application.original import OriginalMailService
from app.features.work_cases.api.router import progress_error, progress_waiting_reason

MAIL = '44444444-4444-4444-4444-444444444444'


def test_hold_does_not_also_report_extraction_failed():
    assert progress_error('ON_HOLD', 'MISSING_FIELD') is None
    assert progress_error('ON_HOLD', 'NO_SUITABLE_CANDIDATES') == {'code': 'NO_SUITABLE_CANDIDATES'}


def test_failed_keeps_safe_codes_and_masks_unknown_ones():
    assert progress_error('FAILED', 'GRAPH_TIME_LIMIT') == {'code': 'GRAPH_TIME_LIMIT'}
    assert progress_error('FAILED', 'raw provider text') == {'code': 'EXTRACTION_FAILED'}
    assert progress_error('FAILED', None) is None


def test_candidate_selection_has_waiting_reason():
    assert progress_waiting_reason('SELECTING_CANDIDATES') == '후보 찾기 대기'
    assert progress_waiting_reason('EXTRACTING') is None


class _Repo:
    def __init__(self, row):
        self.row = row

    def get(self, owner, mail_id):
        return self.row


def _row(provider, attachments):
    return {'id': MAIL, 'subject': 's', 'sender': 'a@example.com', 'recipients_jsonb': ['b@example.com'],
            'sent_or_received_at': '2026-11-09T00:00:00Z', 'body_text': 'b', 'attachments_jsonb': attachments,
            'mail_classification': 'BUSINESS', 'provider': provider}


def test_original_attachment_can_download_only_for_gmail_parts():
    attachments = [{'filename': 'q.pdf', 'mime_type': 'application/pdf', 'size_bytes': 10, 'provider_part_ref': '2'},
                   {'filename': 'x.pdf', 'mime_type': 'application/pdf', 'size_bytes': 5}]
    mail = OriginalMailService(_Repo(_row('GMAIL', attachments))).get('owner', MAIL)
    assert [a.can_download for a in mail.attachments] == [True, False]
    mail = OriginalMailService(_Repo(_row('UPLOAD', attachments[:1]))).get('owner', MAIL)
    assert mail.attachments[0].can_download is False
