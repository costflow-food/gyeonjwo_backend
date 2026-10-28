import base64
import json
from decimal import Decimal
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.core.errors import ServiceError
from app.main import create_app
from app.api.dependencies import require_member
from app.features.review.domain.schemas import FIELD_NAMES

WORK = 'W-20261026-001'


def make_client(*, authorized=True):
    settings = Settings(_env_file=None, worker_enabled=False, gmail_email='purchase@example.com',
                        gmail_subscription='projects/demo/subscriptions/mail', pubsub_audience='https://api.example/push',
                        pubsub_service_account='push@example.iam.gserviceaccount.com')
    service = MagicMock()
    app = create_app(settings, service, push_verifier=lambda token, settings: None)
    if authorized:
        app.dependency_overrides[require_member] = lambda: 'test-user'
    return TestClient(app, raise_server_exceptions=False), service


@pytest.mark.parametrize('method,suffix', [('GET', ''), ('GET', '/files'), ('GET', '/files/mail.eml'),
                                         ('POST', '/reclassify'), ('POST', '/analyze'), ('POST', '/skip'), ('PATCH', '/notes')])
def test_every_business_endpoint_requires_authentication(method, suffix):
    client, service = make_client(authorized=False)
    response = client.request(method, '/api/v1/work-items/' + WORK + suffix, json={})
    assert response.status_code == 401
    service.detail.assert_not_called()
    service.analyze.assert_not_called()


def test_work_detail_does_not_expose_server_credentials():
    client, service = make_client()
    service.detail.return_value = {'work': {'work_id': WORK, 'received_at': '2026-10-26T00:00:00Z', 'sender': 'sender',
        'subject': 'subject', 'classification_reason': None, 'notes': None, 'queue_status': 'PENDING', 'item_id': None,
        'created_at': '2026-10-26T00:00:00Z', 'updated_at': '2026-10-26T00:00:00Z', 'run_id': 'private-token'}, 'mail_header': {'message_id': 'original'}}
    response = client.get('/api/v1/work-items/' + WORK)
    assert response.status_code == 200
    assert response.json()['mail_header']['message_id'] == 'original'
    assert response.headers['cache-control'] == 'no-store'
    assert 'run_id' not in response.json()['work']


@pytest.mark.parametrize('outcome,status', [('SUCCESS', 200), ('REVIEW_REQUIRED', 200), ('FAILED', 502)])
def test_analyze_delivers_current_result_once_with_decimal_strings(outcome, status):
    client, service = make_client()
    amount = {'goods_amount': Decimal('5420000.00'), 'transport_amount': Decimal('0'), 'other_logistics_amount': Decimal('0'),
              'total_cost': Decimal('5420000.00'), 'difference_from_before': Decimal('0')}
    card = {**amount, 'option_id': 'AFTER', 'type': 'AFTER', 'supplier_id': 'supplier', 'supplier_name': 'name', 'reference_id': None,
            'fixed_unit_price': '5000', 'changeable_unit_price': '5500', 'title': 'title', 'explanation': 'explanation', 'formula': 'formula',
            'calculation_status': 'VERIFIED', 'unconfirmed_conditions': [], 'sources': ['source']}
    service.analyze.return_value = status, {'work_id': WORK, 'queue_status': 'COMPLETE', 'outcome': outcome,
        'cards': [card] if outcome == 'SUCCESS' else [], 'issues': [{'code': 'MISSING'}] if outcome == 'REVIEW_REQUIRED' else [],
        'result_persisted': False, 'item_id': 'item', 'unit': 'kg', 'currency': 'KRW', 'period': {'from': '2026-11-01', 'to': '2026-11-30'},
        'quantities': {'total': '1000', 'fixed': '400', 'changeable': '600'}, 'baseline': {'type': 'BEFORE', **amount},
        'extracted_fields': [{'field_name': name, 'raw_value': None, 'source_location': 'source'} for name in FIELD_NAMES],
        'calculation_verification': {'rounds': 3, 'all_rounds_match': True, 'independent_check_passed': True},
        'error': {'code': 'FAILED'} if outcome == 'FAILED' else None}
    response = client.post('/api/v1/work-items/' + WORK + '/analyze', json={})
    assert response.status_code == status
    assert response.json()['baseline']['goods_amount'] == '5420000.00'
    assert response.json()['result_persisted'] is False
    assert client.get('/api/v1/work-items/' + WORK + '/results').status_code == 404


def test_client_cannot_supply_extracts_or_classification():
    client, service = make_client()
    response = client.post('/api/v1/work-items/' + WORK + '/analyze', json={'new_unit_price': 1})
    assert response.status_code == 422
    service.analyze.assert_not_called()


def test_queue_update_failure_does_not_claim_completed_outcome():
    client, service = make_client()
    service.analyze.side_effect = ServiceError('QUEUE_UPDATE_FAILED', '종료 실패', 503)
    response = client.post('/api/v1/work-items/' + WORK + '/analyze', json={})
    assert response.status_code == 503
    assert 'outcome' not in response.json() and 'queue_status' not in response.json()


def test_file_uses_attachment_and_no_sniff_headers():
    client, service = make_client()
    service.file.return_value = {'content_type': 'text/html', 'original_name': '원문.html'}, b'<html>source</html>'
    response = client.get('/api/v1/work-items/' + WORK + '/files/attach_001.html')
    assert response.status_code == 200
    assert response.headers['content-disposition'].startswith('attachment;')
    assert response.headers['x-content-type-options'] == 'nosniff'
    assert 'sandbox' in response.headers['content-security-policy']


def test_notes_limit_and_state_independent_service_call():
    client, service = make_client()
    service.notes.return_value = {'work_id': WORK, 'notes': '특이사항'}
    assert client.patch('/api/v1/work-items/' + WORK + '/notes', json={'notes': '특이사항'}).status_code == 200
    service.notes.assert_called_once_with(WORK, '특이사항')
    assert client.patch('/api/v1/work-items/' + WORK + '/notes', json={'notes': 'a' * 8193}).status_code == 422


def push_body(email='purchase@example.com', history='123'):
    data = base64.b64encode(json.dumps({'emailAddress': email, 'historyId': history}).encode()).decode()
    return {'subscription': 'projects/demo/subscriptions/mail', 'message': {'messageId': 'alert-1', 'data': data}}


def test_webhook_ack_only_after_persistent_enqueue():
    client, service = make_client()
    response = client.post('/api/v1/webhooks/gmail', headers={'Authorization': 'Bearer push'}, json=push_body())
    assert response.status_code == 204
    service.rpc.call.assert_called_once_with('enqueue_gmail_notification', p_subscription='projects/demo/subscriptions/mail',
                                             p_pubsub_message_id='alert-1', p_email='purchase@example.com', p_history_id='123')
    service.rpc.call.side_effect = RuntimeError('database secret should not leak')
    response = client.post('/api/v1/webhooks/gmail', headers={'Authorization': 'Bearer push'}, json=push_body())
    assert response.status_code == 503 and 'secret' not in response.text


@pytest.mark.parametrize('body', [push_body(email='other@example.com'), push_body(history='-1'), [], {'message': {}}])
def test_invalid_or_unexpected_push_never_enqueues(body):
    client, service = make_client()
    response = client.post('/api/v1/webhooks/gmail', headers={'Authorization': 'Bearer push'}, json=body)
    assert response.status_code in (400, 403)
    service.rpc.call.assert_not_called()


def test_not_configured_readiness_is_not_successful():
    client, _ = make_client()
    assert client.get('/health').status_code == 200
    assert client.get('/ready').status_code == 503
