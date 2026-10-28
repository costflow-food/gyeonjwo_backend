from datetime import datetime, timezone
from unittest.mock import MagicMock
from uuid import UUID

import httpx
import pytest
from fastapi.testclient import TestClient

from app.features.auth.application.auth import AccessDenied, verify_member
from app.core.config import Settings
from app.shared.infrastructure.db.contracts import InvalidRpcResponse, RpcRepository, WorkNotFound, WORK_COLUMNS
from app.shared.infrastructure.db.storage import mail_path
from app.main import app


def test_health_is_outside_versioned_api():
    assert TestClient(app).get('/health').status_code == 200
    assert '/health' in app.openapi()['paths']


@pytest.mark.parametrize('response', [None, 'true', 'false', 1, {}, []])
def test_completion_requires_actual_boolean_true(response):
    client = MagicMock()
    client.rpc.return_value.execute.return_value.data = response
    with pytest.raises(InvalidRpcResponse):
        RpcRepository(client).call('complete_analysis', p_work_id='W-20261026-001', p_run_id='run')


def test_rpc_serializes_uuid_and_aware_datetime():
    client = MagicMock()
    client.rpc.return_value.execute.return_value.data = True
    run_id = UUID('00000000-0000-0000-0000-000000000001')
    RpcRepository(client).call('complete_analysis', p_work_id='W-20261026-001', p_run_id=run_id)
    assert client.rpc.call_args.args[1]['p_run_id'] == str(run_id)
    client.rpc.return_value.execute.return_value.data = {'mailbox_id': str(run_id)}
    RpcRepository(client).call('configure_gmail_mailbox', p_email='purchase@example.com',
                               p_watch_expires_at=datetime(2026, 10, 26, tzinfo=timezone.utc))
    assert client.rpc.call_args.args[1]['p_watch_expires_at'] == '2026-10-26T00:00:00+00:00'


def test_naive_timestamp_never_reaches_database():
    client = MagicMock()
    with pytest.raises(ValueError, match='timezone'):
        RpcRepository(client).call('configure_gmail_mailbox', p_email='purchase@example.com',
                                   p_watch_expires_at=datetime(2026, 10, 26))
    client.rpc.assert_not_called()


@pytest.mark.parametrize('payload', [[], None, {'id': 123}, {'id': ' '}])
def test_malformed_auth_response_fails_closed(payload):
    settings = Settings(_env_file=None, supabase_url='https://example.supabase.co',
                        supabase_publishable_key='test')
    def handler(request):
        assert request.url.path == '/auth/v1/user'
        return httpx.Response(200, json=payload) if payload is not None else httpx.Response(200, content='null')
    with pytest.raises(AccessDenied):
        verify_member('token', settings, transport=httpx.MockTransport(handler))


def test_work_id_rejects_non_ascii_digits():
    with pytest.raises(ValueError):
        mail_path('W-２０２６１０２６-００１')


def test_latest_queue_schema_has_notes_and_fourteen_columns():
    assert len(WORK_COLUMNS.split(',')) == 14
    assert 'notes' in WORK_COLUMNS.split(',')


@pytest.mark.parametrize('notes', [None, '', '   ', ' 앞뒤 공백\n유지 ', 'a' * 8192])
def test_notes_rpc_preserves_input_and_uses_correct_contract(notes):
    client = MagicMock()
    client.rpc.return_value.execute.return_value.data = True
    assert RpcRepository(client).call('update_work_item_notes', p_work_id='W-20261026-001', p_notes=notes) is True
    assert client.rpc.call_args.args[1]['p_notes'] == notes


@pytest.mark.parametrize('notes', [False, 123, 'a' * 8193])
def test_invalid_notes_are_rejected_before_network(notes):
    client = MagicMock()
    with pytest.raises(ValueError):
        RpcRepository(client).call('update_work_item_notes', p_work_id='W-20261026-001', p_notes=notes)
    client.rpc.assert_not_called()


def test_notes_false_means_missing_work_not_lost_analysis_ownership():
    client = MagicMock()
    client.rpc.return_value.execute.return_value.data = False
    with pytest.raises(WorkNotFound):
        RpcRepository(client).call('update_work_item_notes', p_work_id='W-20261026-001')


def test_empty_attachment_is_allowed_but_empty_mail_is_not():
    import hashlib
    from app.shared.infrastructure.db.storage import SourceStorage
    client = MagicMock()
    client.storage.from_.return_value.download.return_value = b''
    storage = SourceStorage(client)
    checksum = hashlib.sha256(b'').hexdigest()
    assert storage.download_verified('W-20261026-001/attach_001.txt', 0, checksum) == b''
    with pytest.raises(ValueError):
        storage.download_verified('W-20261026-001/mail.eml', 0, checksum)


def test_example_configuration_can_have_empty_optional_credentials():
    settings = Settings(_env_file=None, llm_base_url='', llm_api_key='', gmail_refresh_token='')
    assert settings.llm_base_url is None and settings.gmail_refresh_token is None
