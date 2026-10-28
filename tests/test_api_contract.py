"""08 공통 계약: 응답 봉투·오류 형식·request_id·CORS·요청 제한."""
from unittest.mock import MagicMock

from fastapi.testclient import TestClient

from app.api.dependencies import require_member
from app.core.config import Settings
from app.core.errors import ServiceError
from app.main import RATE_LIMIT_PER_WINDOW, create_app

WORK = 'W-20261026-001'
ORIGIN = 'https://web.example'
ERROR_KEYS = {'code', 'message', 'fields', 'current_revision', 'retryable', 'next_action'}


def make_client(*, authorized=True):
    settings = Settings(_env_file=None, worker_enabled=False, cors_origins=[ORIGIN], gmail_email='purchase@example.com',
                        gmail_subscription='projects/demo/subscriptions/mail', pubsub_audience='https://api.example/push',
                        pubsub_service_account='push@example.iam.gserviceaccount.com')
    service = MagicMock()
    app = create_app(settings, service, push_verifier=lambda token, settings: None)
    if authorized:
        app.dependency_overrides[require_member] = lambda: 'test-user'
    return TestClient(app, raise_server_exceptions=False), service


def assert_error(response, status, code):
    body = response.json()
    assert response.status_code == status
    assert set(body) == {'error', 'meta'}
    assert set(body['error']) == ERROR_KEYS
    assert body['error']['code'] == code
    assert body['meta']['request_id'] == response.headers['x-request-id']
    return body['error']


def test_success_response_uses_data_and_meta_envelope():
    client, service = make_client()
    service.notes.return_value = {'work_id': WORK, 'notes': '메모'}
    response = client.patch('/api/v1/work-items/' + WORK + '/notes', json={'notes': '메모'})
    body = response.json()
    assert response.status_code == 200
    assert set(body) == {'data', 'meta'}
    assert body['data'] == {'work_id': WORK, 'notes': '메모'}
    assert body['meta']['request_id'].startswith('req-')
    assert body['meta']['request_id'] == response.headers['x-request-id']


def test_each_request_gets_a_distinct_request_id():
    client, _ = make_client()
    first, second = client.get('/health'), client.get('/health')
    assert first.headers['x-request-id'] != second.headers['x-request-id']


def test_missing_login_is_auth_required():
    client, _ = make_client(authorized=False)
    error = assert_error(client.get('/api/v1/work-items/' + WORK), 401, 'AUTH_REQUIRED')
    assert error['retryable'] is False


def test_unknown_route_uses_contract_not_framework_default():
    client, _ = make_client()
    assert_error(client.get('/api/v1/does-not-exist'), 404, 'RESOURCE_NOT_FOUND')


def test_validation_error_lists_fields():
    client, _ = make_client()
    error = assert_error(client.patch('/api/v1/work-items/' + WORK + '/notes', json={'notes': 'a' * 8193}), 422, 'INVALID_REQUEST')
    assert 'body.notes' in error['fields']


def test_state_conflict_suggests_refresh():
    client, service = make_client()
    service.skip.side_effect = ServiceError('ACTION_NOT_ALLOWED', '분석 전 업무만 가능합니다.', 409)
    error = assert_error(client.post('/api/v1/work-items/' + WORK + '/skip', json={}), 409, 'ACTION_NOT_ALLOWED')
    assert error['next_action'] == 'REFRESH_CASE' and error['retryable'] is False


def test_unexpected_failure_is_retryable_processing_unavailable():
    client, service = make_client()
    service.files.side_effect = RuntimeError('boom')
    error = assert_error(client.get('/api/v1/work-items/' + WORK + '/files'), 503, 'PROCESSING_UNAVAILABLE')
    assert error['retryable'] is True


def test_oversized_body_uses_error_contract():
    client, service = make_client()
    response = client.patch('/api/v1/work-items/' + WORK + '/notes', content=b'{"notes":"' + b'a' * 70000 + b'"}',
                            headers={'Content-Type': 'application/json'})
    assert_error(response, 413, 'REQUEST_TOO_LARGE')
    service.notes.assert_not_called()


def test_cors_preflight_allows_put_and_idempotency_key():
    client, _ = make_client()
    response = client.options('/api/v1/work-items/' + WORK + '/notes', headers={
        'Origin': ORIGIN, 'Access-Control-Request-Method': 'PUT',
        'Access-Control-Request-Headers': 'authorization,content-type,idempotency-key'})
    assert response.status_code == 200
    assert 'PUT' in response.headers['access-control-allow-methods']
    assert 'idempotency-key' in response.headers['access-control-allow-headers'].lower()


def test_cors_exposes_request_id_and_retry_after():
    client, _ = make_client()
    response = client.get('/health', headers={'Origin': ORIGIN})
    exposed = response.headers['access-control-expose-headers'].lower()
    assert 'x-request-id' in exposed and 'retry-after' in exposed


def test_rate_limit_returns_retry_after_and_cors_headers():
    client, _ = make_client()
    for _ in range(RATE_LIMIT_PER_WINDOW):
        assert client.get('/health').status_code == 200
    response = client.get('/health', headers={'Origin': ORIGIN})
    assert_error(response, 429, 'RATE_LIMITED')
    assert 1 <= int(response.headers['retry-after']) <= 60
    assert response.headers['access-control-allow-origin'] == ORIGIN


def test_gmail_webhook_is_not_rate_limited():
    client, service = make_client()
    for _ in range(RATE_LIMIT_PER_WINDOW):
        client.get('/health')
    response = client.post('/api/v1/webhooks/gmail', json={}, headers={'Authorization': 'Bearer google'})
    assert response.status_code != 429


def test_ops_health_stays_plain_for_container_healthcheck():
    client, _ = make_client()
    assert client.get('/health').json()['status'] == 'ok'
    assert client.get('/api/v1/health').status_code == 404
