import hashlib
from datetime import date
from decimal import Decimal as D
from email.message import EmailMessage
from types import SimpleNamespace
from unittest.mock import MagicMock

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.core.config import Settings
from app.shared.infrastructure.db.contracts import InvalidRpcResponse, RpcRepository
from app.core.errors import ReviewRequired, ServiceError
from app.features.mail.infrastructure.gmail import GmailClient
from app.main import create_app
from app.api.dependencies import require_member
from app.features.review.domain.schemas import Extraction, ExtractedField, FIELD_NAMES
from app.features.review.application.pipeline import cost_value, request_costs
from app.features.review.infrastructure.reference_files import reference_id
from app.features.review.application.services import Services
from app.features.mail.application.worker import GmailWorker

START, END, WORK = date(2026, 1, 1), date(2026, 1, 31), 'W-20261026-001'


def fields(basis):
    return {'cost_basis': basis, 'cost_payer': 'BUYER', 'cost_included_in_price': 'FALSE',
            'transport_amount': '100원', 'other_logistics_amount': '20원'}


@pytest.mark.parametrize('basis', ['기존 계약과 동일하지 않음', '기존과 동일하진 않음', '기존 비용과 다름'])
def test_negated_equality_does_not_inherit_contract_costs(basis):
    with pytest.raises(ReviewRequired):
        request_costs(fields(basis), {'TRANSPORT': D('100'), 'OTHER_LOGISTICS': D('20')}, D('1000'), START, END)


def test_exact_equality_cannot_hide_changed_payer():
    values = fields('기존 계약과 동일')
    values['cost_payer'] = 'SUPPLIER'
    with pytest.raises(ReviewRequired) as error:
        request_costs(values, {'TRANSPORT': D('100'), 'OTHER_LOGISTICS': D('20')}, D('1000'), START, END)
    assert error.value.issue['code'] == 'COST_CONFLICT'


@pytest.mark.parametrize('basis', ['운송비 기존과 동일', '기존 계약과 동일, 기타 물류비 미확인', '일부 비용은 기존과 동일'])
def test_partial_cost_equality_cannot_confirm_both_cost_kinds(basis):
    with pytest.raises(ReviewRequired):
        request_costs(fields(basis), {'TRANSPORT': D('100'), 'OTHER_LOGISTICS': D('20')}, D('1000'), START, END)


@pytest.mark.parametrize('basis', ['2026년 11월 월정액 1000kg', '2026년 1월 1일~15일 월정액 1000kg',
                                  '2026년 1월 및 2026년 2월 월정액 1000kg'])
def test_month_must_match_exactly_and_no_partial_month_allocation(basis):
    with pytest.raises(ReviewRequired):
        request_costs(fields(basis), {}, D('1000'), START, END)


def test_supported_explicit_monthly_cost():
    assert request_costs(fields('2026년 1월 월정액 1000kg'), {}, D('1000'), START, END) == {'TRANSPORT': D('100'), 'OTHER_LOGISTICS': D('20')}


def test_per_unit_cost_coverage_quantity_is_checked():
    cost = {'payer': 'BUYER', 'included_in_price': 'FALSE', 'amount': D('10'), 'basis': 'PER_UNIT',
            'coverage_from': None, 'coverage_to': None, 'coverage_quantity': D('1000')}
    assert cost_value(cost, D('600'), START, END) is None


@pytest.mark.parametrize('name,result,params', [
    ('enqueue_gmail_notification', None, {'p_subscription': 'sub', 'p_pubsub_message_id': 'id', 'p_email': 'mail', 'p_history_id': '1'}),
    ('enqueue_gmail_notification', True, {'p_subscription': 'sub', 'p_pubsub_message_id': 'id', 'p_email': 'mail', 'p_history_id': '1'}),
    ('start_analysis', {'run_id': 'missing-work-and-lease'}, {'p_work_id': WORK}),
    ('register_work_item', {'created': 'true', 'work_id': WORK}, {key: 'value' for key in 'p_work_id p_mailbox_id p_gmail_message_id p_received_at p_sender p_subject p_mail_body p_attachments p_raw_size_bytes p_raw_sha256'.split()}),
    ('issue_work_id', 'not-work-id', {}),
    ('recover_expired_analyses', -1, {}),
])
def test_incompatible_rpc_data_never_becomes_success(name, result, params):
    client = MagicMock()
    client.rpc.return_value.execute.return_value.data = result
    with pytest.raises(InvalidRpcResponse):
        RpcRepository(client).call(name, **params)


def test_large_numeric_history_is_preserved_without_float_conversion():
    client = MagicMock()
    client.rpc.return_value.execute.return_value.data = {'notification_id': 1, 'mailbox_id': '00000000-0000-0000-0000-000000000001',
        'email_address': 'purchase@example.com', 'last_history_id': 123456789012345678901234567890,
        'target_history_id': '123456789012345678901234567891', 'claim_token': '00000000-0000-0000-0000-000000000002',
        'lease_expires_at': '2026-10-26T00:00:00Z', 'attempt_count': 1}
    result = RpcRepository(client).call('claim_gmail_notification')
    assert result['last_history_id'] == '123456789012345678901234567890'


def test_wrong_oauth_mailbox_is_rejected_before_watch():
    calls = []
    def handler(request):
        calls.append(request.url.path)
        return httpx.Response(200, json={'emailAddress': 'wrong@example.com', 'historyId': '1'})
    configured = Settings(_env_file=None, gmail_email='purchase@example.com', gmail_client_id='client', gmail_client_secret='secret',
        gmail_refresh_token='refresh', gmail_topic='topic', gmail_subscription='subscription', pubsub_audience='audience', pubsub_service_account='caller')
    gmail = GmailClient(configured, httpx.MockTransport(handler))
    gmail.credentials = SimpleNamespace(valid=True, token='test')
    with pytest.raises(ServiceError) as error:
        gmail.watch()
    assert error.value.code == 'GMAIL_MAILBOX_MISMATCH'
    assert calls == ['/gmail/v1/users/me/profile']


def test_watch_failure_does_not_block_receiving_persisted_notifications():
    service = MagicMock()
    service.settings = Settings(_env_file=None)
    worker = GmailWorker(service)
    worker.maintenance = MagicMock(side_effect=RuntimeError('watch failed'))
    def process():
        worker.stop.set()
        return True
    worker.process_once = MagicMock(side_effect=process)
    worker.start()
    worker.thread.join(timeout=2)
    worker.process_once.assert_called_once()


def test_analysis_recovery_is_attempted_before_gmail_watch_failure():
    service = MagicMock()
    service.settings = Settings(_env_file=None)
    service.gmail.watch.side_effect = RuntimeError('watch failed')
    with pytest.raises(RuntimeError):
        GmailWorker(service).maintenance()
    service.rpc.call.assert_any_call('recover_expired_analyses')


def test_source_text_must_match_verified_original():
    message = EmailMessage()
    message['From'], message['Subject'] = 'from@example.com', '제목'
    message.set_content('실제 원문')
    service = Services(Settings(_env_file=None), MagicMock())
    service.raw = MagicMock(return_value=message.as_bytes())
    with pytest.raises(ServiceError) as error:
        service.verify_source(WORK, {'mail_body': '변조된 본문'})
    assert error.value.code == 'SOURCE_MISMATCH'


def test_reference_download_is_scoped_to_this_work_allow_list():
    service = Services(Settings(_env_file=None), MagicMock())
    service.reference_files = MagicMock(return_value={'files': [{'file_id': reference_id('contract.txt'), 'source_ref': 'contract.txt', 'filename': 'contract.txt'}]})
    with pytest.raises(ServiceError) as error:
        service.reference_file(WORK, reference_id('other-contract.txt'))
    assert error.value.status == 404
    service.client.storage.from_.return_value.download.assert_not_called()


def test_chunked_oversized_json_is_rejected_before_parsing():
    service = MagicMock()
    app = create_app(Settings(_env_file=None), service)
    app.dependency_overrides[require_member] = lambda: 'test-user'
    response = TestClient(app).post('/api/v1/work-items/' + WORK + '/notes',
                                    content=iter([b'{"notes":"', b'a' * 70000, b'"}']), headers={'Content-Type': 'application/json'})
    assert response.status_code == 413
    service.notes.assert_not_called()


def test_invalid_public_analysis_output_is_not_published_as_success():
    service = MagicMock()
    service.analyze.return_value = 200, {'work_id': WORK, 'queue_status': 'COMPLETE', 'outcome': 'SUCCESS', 'cards': [], 'issues': [], 'result_persisted': False}
    app = create_app(Settings(_env_file=None), service)
    app.dependency_overrides[require_member] = lambda: 'test-user'
    response = TestClient(app, raise_server_exceptions=False).post('/api/v1/work-items/' + WORK + '/analyze', json={})
    assert response.status_code == 503 and response.json()['error']['code'] == 'SERVICE_UNAVAILABLE'
