import base64
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.core.errors import ServiceError
from app.features.mailboxes.application.ingestion import determine_link, message_metadata
from app.features.mailboxes.application.worker import ConnectedMailboxWorker
from app.features.mailboxes.infrastructure.google import GoogleAPI, Grant
from app.features.mailboxes.domain.schemas import READ_SCOPE, SEND_SCOPE
from app.main import create_app


def raw_message(mime='text/plain', text='견적 요청'):
    return {'id': 'm-1', 'threadId': 't-1', 'internalDate': '1793142000000', 'payload': {
        'mimeType': 'multipart/mixed', 'headers': [{'name': 'Subject', 'value': '요청'},
        {'name': 'From', 'value': 'Supplier <supplier@example.com>'},
        {'name': 'To', 'value': 'member@example.com'}, {'name': 'Message-ID', 'value': '<new@example.com>'}],
        'parts': [{'mimeType': mime, 'body': {'data': base64.urlsafe_b64encode(text.encode()).decode().rstrip('=')}},
            {'partId': '2', 'filename': 'quote.pdf', 'mimeType': 'application/pdf',
             'body': {'attachmentId': 'attachment-a', 'size': 200, 'data': 'MUST_NOT_DECODE_ATTACHMENT'}}]}}


def test_metadata_preserves_gmail_attachment_reference_without_body_download():
    result = message_metadata(raw_message())
    assert result['body_text'] == '견적 요청'
    assert result['attachments'] == [{'gmail_attachment_id': 'attachment-a', 'part_id': '2',
        'filename': 'quote.pdf', 'mime_type': 'application/pdf', 'size_bytes': 200}]
    assert result['sender'] == ['supplier@example.com']
    assert result['received_at'].endswith('Z')


def test_html_only_mail_yields_text_without_script_or_style_execution():
    result = message_metadata(raw_message('text/html', '<p>견적 안내</p><script>secret()</script><style>hidden</style>'))
    assert '견적 안내' in result['body_text']
    assert 'secret' not in result['body_text'] and 'hidden' not in result['body_text']


def test_declared_korean_mime_charset_is_used():
    message = raw_message()
    part = message['payload']['parts'][0]
    part['headers'] = [{'name': 'Content-Type', 'value': 'text/plain; charset=euc-kr'}]
    part['body']['data'] = base64.urlsafe_b64encode('견적 요청'.encode('euc-kr')).decode()
    assert message_metadata(message)['body_text'] == '견적 요청'


def test_reply_requires_real_header_reference_and_same_participants():
    case = str(uuid4())
    message = {'in_reply_to': '<original@example.com>', 'references': None}
    candidate = {'case_id': case, 'same_participants': True, 'matched_reference': '<original@example.com>'}
    assert determine_link(message, [candidate])['case_id'] == case
    assert determine_link(message, [candidate | {'same_participants': False}])['kind'] == 'AMBIGUOUS'
    assert determine_link(message, [candidate | {'matched_reference': '<other@example.com>'}])['kind'] == 'AMBIGUOUS'
    assert determine_link(message, [candidate, candidate | {'case_id': str(uuid4())}])['kind'] == 'AMBIGUOUS'
    assert determine_link(message, [])['kind'] == 'AMBIGUOUS'
    assert determine_link({}, [])['kind'] == 'NEW_MESSAGE'


def worker_fixture(classification='BUSINESS', existing=False):
    repo, service = MagicMock(), MagicMock()
    service.repo = repo
    service.settings = Settings(_env_file=None)
    job = {'job_id': str(uuid4()), 'claim_token': str(uuid4()), 'user_id': str(uuid4()),
           'mailbox_ref': str(uuid4()), 'connection_revision': 1, 'last_history_id': '100'}
    repo.claim_sync.return_value = job
    repo.message_exists.return_value = existing
    repo.reply_candidates.return_value = []
    repo.finish_sync.return_value = True
    row = {'mailbox_ref': job['mailbox_ref'], 'connection_revision': 1,
           'public': {'watch': {'started': False, 'expiration': None}}}
    service.authorized.return_value = row, object()
    service.google.message_ids.return_value = ['m-1'], '101'
    service.google.message.return_value = raw_message()
    classifier = MagicMock(return_value={'status': classification, 'reason': '테스트 분류'})
    return ConnectedMailboxWorker(service, classifier), repo, service, classifier, job


@pytest.mark.parametrize('status', ['BUSINESS', 'NON_BUSINESS', 'UNCERTAIN'])
def test_classification_is_saved_before_cursor_advances(status):
    worker, repo, _, classifier, job = worker_fixture(status)
    calls = []
    repo.ingest.side_effect = lambda *args: calls.append('stored')
    repo.finish_sync.side_effect = lambda *args: calls.append('cursor') or True
    assert worker.process_once() is True
    assert repo.ingest.call_args.args[2]['status'] == status
    assert repo.ingest.call_args.args[3]['kind'] == 'NEW_MESSAGE'
    assert calls == ['stored', 'cursor']
    classifier.assert_called_once()


def test_duplicate_message_does_not_call_google_or_classifier_again():
    worker, repo, service, classifier, _ = worker_fixture(existing=True)
    worker.process_once()
    service.google.message.assert_not_called()
    classifier.assert_not_called()
    repo.ingest.assert_not_called()
    repo.finish_sync.assert_called_once()


def test_classifier_failure_is_not_nonbusiness_and_does_not_advance_cursor():
    worker, repo, _, classifier, _ = worker_fixture()
    classifier.side_effect = RuntimeError('secret mailbox body')
    worker.process_once()
    repo.ingest.assert_not_called()
    repo.finish_sync.assert_not_called()
    repo.fail_sync.assert_called_once()


def test_storage_failure_does_not_advance_cursor():
    worker, repo, _, _, _ = worker_fixture()
    repo.ingest.side_effect = ServiceError('INGEST_UNAVAILABLE', '저장 실패', 503)
    worker.process_once()
    repo.finish_sync.assert_not_called()
    repo.fail_sync.assert_called_once()


def test_no_new_mail_is_successful_sync_and_does_not_fabricate_work():
    worker, repo, service, classifier, _ = worker_fixture()
    service.google.message_ids.return_value = [], '100'
    worker.process_once()
    repo.ingest.assert_not_called()
    classifier.assert_not_called()
    repo.finish_sync.assert_called_once()


def test_expired_history_reconciles_from_full_listing():
    worker, repo, service, _, _ = worker_fixture()
    service.google.message_ids.side_effect = [ServiceError('GMAIL_NOT_FOUND', '커서 만료', 404), ([], '150')]
    worker.process_once()
    assert service.google.message_ids.call_args.args[1] is None
    assert repo.finish_sync.call_args.args[1] == '150'


def test_reconnect_during_sync_cannot_commit_under_old_connection_revision():
    worker, repo, service, classifier, job = worker_fixture()
    service.authorized.return_value[0]['connection_revision'] = 2
    worker.process_once()
    repo.ingest.assert_not_called()
    repo.finish_sync.assert_not_called()


def test_verified_reply_bypasses_short_reply_classification():
    worker, repo, service, classifier, _ = worker_fixture()
    message = raw_message()
    message['payload']['headers'].append({'name': 'In-Reply-To', 'value': '<rfq@example.com>'})
    service.google.message.return_value = message
    repo.reply_candidates.return_value = [{'case_id': str(uuid4()), 'same_participants': True, 'matched_reference': '<rfq@example.com>'}]
    worker.process_once()
    classifier.assert_not_called()
    assert repo.ingest.call_args.args[3]['kind'] == 'EXISTING_CASE'
    assert repo.ingest.call_args.args[2] is None


def test_new_connected_webhook_only_acks_after_durable_enqueue():
    settings = Settings(_env_file=None, google_mailbox_worker_enabled=True,
        gmail_subscription='projects/demo/subscriptions/gmail', pubsub_audience='https://api.example/push',
        pubsub_service_account='push@example.com')
    service = MagicMock()
    app = create_app(settings, push_verifier=lambda *args: None, mailbox_service=service)
    client = TestClient(app)
    import json
    payload = {'subscription': settings.gmail_subscription, 'message': {'messageId': 'push-1', 'data':
        base64.urlsafe_b64encode(json.dumps({'emailAddress': 'member@example.com', 'historyId': '101'}).encode()).decode()}}
    response = client.post('/api/v1/webhooks/gmail', json=payload, headers={'Authorization': 'Bearer google-oidc'})
    assert response.status_code == 204
    service.repo.enqueue_push.assert_called_once_with(settings.gmail_subscription, 'push-1', 'member@example.com', '101')
    service.repo.enqueue_push.side_effect = ServiceError('INGEST_UNAVAILABLE', '저장 실패', 503)
    assert client.post('/api/v1/webhooks/gmail', json=payload, headers={'Authorization': 'Bearer google-oidc'}).status_code == 503


def test_history_listing_reads_all_pages_and_keeps_highest_cursor():
    calls = []
    def transport(method, host, path, headers, body):
        calls.append(path)
        if 'pageToken=' not in path:
            return 200, {'historyId': '120', 'nextPageToken': 'page-2',
                'history': [{'messagesAdded': [{'message': {'id': 'm-1', 'labelIds': ['INBOX']}}]}]}
        return 200, {'historyId': '130', 'history': [{'messagesAdded': [{'message': {'id': 'm-2', 'labelIds': ['INBOX']}}]}]}
    api = GoogleAPI(Settings(_env_file=None), transport)
    grant = Grant('access', 'subject', 'member@example.com', frozenset({READ_SCOPE}))
    assert api.message_ids(grant, '100') == (['m-1', 'm-2'], '130')
    assert len(calls) == 2 and 'pageToken=page-2' in calls[1]


def test_send_scope_alone_does_not_permit_read_or_unapproved_send():
    api = GoogleAPI(Settings(_env_file=None), MagicMock())
    grant = Grant('access', 'subject', 'member@example.com', frozenset({SEND_SCOPE}))
    with pytest.raises(ServiceError, match='GMAIL_SCOPE_REQUIRED'):
        api.profile(grant)
    with pytest.raises(ServiceError, match='ACTION_NOT_ALLOWED'):
        api.gmail(grant, 'messages/send', method='POST', payload={})
    api.transport.assert_not_called()


def test_query_string_cannot_bypass_unapproved_send_guard():
    api = GoogleAPI(Settings(_env_file=None), MagicMock())
    grant = Grant('access', 'subject', 'member@example.com', frozenset({READ_SCOPE, SEND_SCOPE}))
    with pytest.raises(ServiceError, match='ACTION_NOT_ALLOWED'):
        api.gmail(grant, 'messages/send?alt=json', method='POST', payload={})
    api.transport.assert_not_called()
