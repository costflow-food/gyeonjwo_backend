import base64
import json
from datetime import datetime, timezone
from email.message import EmailMessage
from types import SimpleNamespace
from unittest.mock import MagicMock

import httpx
import pytest

from app.core.config import Settings
from app.features.review.infrastructure.documents import parse_mail, source_file
from app.core.errors import ServiceError
from app.features.mail.infrastructure.gmail import GmailClient, parse_notification, verify_push
from app.features.mail.domain.schemas import Classification
from app.features.mail.application.worker import GmailWorker


def settings():
    return Settings(_env_file=None, gmail_email='purchase@example.com', gmail_client_id='client', gmail_client_secret='secret',
                    gmail_refresh_token='refresh', gmail_topic='projects/demo/topics/mail',
                    gmail_subscription='projects/demo/subscriptions/mail', pubsub_audience='audience', pubsub_service_account='caller')


def make_mail():
    message = EmailMessage()
    message['From'], message['Subject'], message['Message-ID'] = 'sales@example.com', '단가 변경 통보', '<original@example.com>'
    message.set_content('단가 변경 원문 본문')
    message.add_attachment(b'example', maintype='application', subtype='pdf', filename='가격통보.pdf')
    return message.as_bytes()


def test_mime_decode_preserves_body_original_filename_and_headers():
    parsed = parse_mail(make_mail())
    assert parsed['subject'] == '단가 변경 통보'
    assert '원문 본문' in parsed['body']
    assert parsed['attachments'][0]['original_name'] == '가격통보.pdf'
    assert parsed['headers']['message_id'] == '<original@example.com>'


def test_html_mail_drops_scripts_and_does_not_invent_missing_subject():
    message = EmailMessage()
    message['From'], message['Subject'] = 'a@example.com', '안내'
    message.set_content('<p>원문</p><script>secret()</script>', subtype='html')
    assert 'secret' not in parse_mail(message.as_bytes())['body']
    del message['Subject']
    with pytest.raises(ValueError):
        parse_mail(message.as_bytes())


def test_attachment_must_belong_to_current_work():
    source = {'attachments': [{'storage_path': 'W-20261026-002/attach_001.pdf'}]}
    with pytest.raises(ServiceError):
        source_file(source, 'W-20261026-001', 'attach_001.pdf')


def test_gmail_history_reads_every_page_and_deduplicates():
    pages = []
    def handler(request):
        if request.url.path.endswith('/profile'):
            return httpx.Response(200, json={'emailAddress': 'purchase@example.com', 'historyId': '100'})
        pages.append(dict(request.url.params))
        if request.url.params.get('pageToken') == 'next':
            return httpx.Response(200, json={'historyId': '200', 'history': [{'messagesAdded': [{'message': {'id': 'm2', 'labelIds': ['INBOX']}}]}]})
        return httpx.Response(200, json={'historyId': '180', 'nextPageToken': 'next', 'history': [{'messagesAdded': [
            {'message': {'id': 'm1', 'labelIds': ['INBOX']}}, {'message': {'id': 'sent', 'labelIds': ['SENT']}}]}]})
    gmail = GmailClient(settings(), httpx.MockTransport(handler))
    gmail.credentials = SimpleNamespace(valid=True, token='access')
    assert gmail.changed_ids('100') == (['m1', 'm2'], '200')
    assert len(pages) == 2 and pages[1]['startHistoryId'] == '100'


def test_full_sync_captures_cursor_before_listing():
    calls = []
    def handler(request):
        calls.append(request.url.path)
        return httpx.Response(200, json={'historyId': '100', 'emailAddress': 'purchase@example.com'} if request.url.path.endswith('/profile') else {'messages': [{'id': 'm1'}]})
    gmail = GmailClient(settings(), httpx.MockTransport(handler))
    gmail.credentials = SimpleNamespace(valid=True, token='access')
    assert gmail.full_ids() == (['m1'], '100')
    assert calls[0].endswith('/profile')


def test_notification_is_not_a_mail_and_history_id_stays_string():
    configured = settings()
    data = base64.b64encode(json.dumps({'emailAddress': configured.gmail_email, 'historyId': '12345678901234567890'}).encode()).decode()
    assert parse_notification({'subscription': configured.gmail_subscription, 'message': {'data': data, 'messageId': 'pubsub-id'}}, configured)[2] == '12345678901234567890'


def test_pubsub_caller_claim_is_checked_even_after_valid_signature(monkeypatch):
    monkeypatch.setattr('app.features.mail.infrastructure.gmail.id_token.verify_oauth2_token', lambda *a: {'email': 'wrong', 'email_verified': True})
    with pytest.raises(ServiceError) as error:
        verify_push('token', settings())
    assert error.value.status == 401


def worker_service():
    service = MagicMock()
    service.settings = settings()
    service.storage.upload_mail.return_value = {'raw_size_bytes': 10, 'raw_sha256': 'a' * 64}
    service.storage.upload_attachment.return_value = {'storage_path': 'W-20261026-001/attach_001.pdf'}
    service.gmail.raw_mail.return_value = make_mail(), datetime(2026, 10, 26, tzinfo=timezone.utc)
    return service


def test_duplicate_mail_does_not_upload_again():
    service = worker_service()
    service.rpc.call.return_value = 'existing-work'
    GmailWorker(service).ingest({'mailbox_id': 'mailbox'}, 'gmail-message')
    service.storage.upload_mail.assert_not_called()
    service.gmail.raw_mail.assert_not_called()


def test_upload_precedes_transactional_registration_and_classification():
    service = worker_service()
    calls = []
    def rpc(name, **params):
        calls.append(name)
        if name == 'find_work_item':
            return None
        if name == 'issue_work_id':
            return 'W-20261026-001'
        if name == 'register_work_item':
            service.storage.upload_mail.assert_called_once()
            service.storage.upload_attachment.assert_called_once()
            return {'created': True, 'work_id': params['p_work_id'], 'classification_token': 'token'}
        return True
    service.rpc.call.side_effect = rpc
    GmailWorker(service).ingest({'mailbox_id': 'mailbox'}, 'gmail-message')
    assert calls == ['find_work_item', 'issue_work_id', 'register_work_item', 'finish_classification']


def test_expired_history_triggers_full_sync_and_completes_only_after_ingest():
    service = worker_service()
    claim = {'notification_id': 1, 'claim_token': 'claim', 'mailbox_id': 'mailbox', 'last_history_id': '1', 'attempt_count': 1}
    service.rpc.call.side_effect = lambda name, **kwargs: claim if name == 'claim_gmail_notification' else True
    service.gmail.changed_ids.side_effect = ServiceError('GMAIL_NOT_FOUND', 'expired', 404)
    service.gmail.full_ids.return_value = ['m1', 'm2'], '200'
    worker = GmailWorker(service)
    worker.ingest = MagicMock()
    assert worker.process_once() is True
    assert worker.ingest.call_count == 2
    service.rpc.call.assert_any_call('finish_gmail_notification', p_notification_id=1, p_claim_token='claim', p_last_history_id='200')


def test_failed_ingest_never_advances_mailbox_cursor():
    service = worker_service()
    claim = {'notification_id': 1, 'claim_token': 'claim', 'mailbox_id': 'mailbox', 'last_history_id': '1', 'attempt_count': 1}
    service.rpc.call.side_effect = lambda name, **kwargs: claim if name == 'claim_gmail_notification' else True
    service.gmail.changed_ids.return_value = ['m1'], '200'
    worker = GmailWorker(service)
    worker.stop.set()  # avoid actual backoff wait in this test
    worker.ingest = MagicMock(side_effect=RuntimeError('upload failure'))
    worker.process_once()
    assert not any(call.args[0] == 'finish_gmail_notification' for call in service.rpc.call.call_args_list)
    service.rpc.call.assert_any_call('fail_gmail_notification', p_notification_id=1, p_claim_token='claim', p_error_code='GMAIL_SYNC_FAILED', p_retry=True)
