"""No real Google/Supabase requests. In-memory repository below is a test double only."""
import copy
import threading
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock
from uuid import uuid4

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.core.config import Settings
from app.core.errors import ServiceError
from app.features.mailboxes.api.router import require_google_user
from app.features.mailboxes.application.auth import google_principal
from app.features.mailboxes.application.service import GoogleConnectionService
from app.features.mailboxes.domain.schemas import GoogleConnectRequest, GooglePrincipal, READ_SCOPE, SEND_SCOPE
from app.features.mailboxes.infrastructure.crypto import TokenCipher
from app.features.mailboxes.infrastructure.google import GoogleAPI, GoogleRejected, Grant
from app.main import create_app

USER = str(uuid4())
PRINCIPAL = GooglePrincipal(USER, 'google-subject-a', 'member@example.com')
TOKENS = {'provider_access_token': 'access-secret', 'provider_refresh_token': 'refresh-secret'}


class FakeRepository:
    def __init__(self):
        self.records, self.connection, self.jobs = {}, None, []
        self.lock = threading.Lock()
        self.mailbox_ref = str(uuid4())

    def claim(self, user_id, key, body_hash):
        with self.lock:
            record = self.records.get((user_id, key))
            if record:
                if record['body_hash'] != body_hash:
                    return {'kind': 'CONFLICT'}
                if record.get('response'):
                    return {'kind': 'REPLAY', 'response': copy.deepcopy(record['response'])}
                return {'kind': 'IN_PROGRESS'}
            record = {'request_id': str(uuid4()), 'body_hash': body_hash}
            self.records[user_id, key] = record
            return {'kind': 'ACQUIRED', 'request_id': record['request_id'], 'mailbox_ref': self.mailbox_ref}

    def finish(self, request_id, user_id, response, connection):
        with self.lock:
            record = next(row for (owner, _), row in self.records.items() if owner == user_id and row['request_id'] == request_id)
            record['response'] = copy.deepcopy(response)
            if connection:
                self.connection = copy.deepcopy(connection) | {'user_id': user_id, 'connection_revision': 1}
                if connection['sync_requested']:
                    self.jobs.append(connection['mailbox_ref'])

    def retryable(self, request_id, user_id):
        with self.lock:
            for key, record in list(self.records.items()):
                if key[0] == user_id and record['request_id'] == request_id:
                    del self.records[key]

    def get(self, user_id):
        return copy.deepcopy(self.connection) if self.connection and self.connection['user_id'] == user_id else None

    def mark_reconnect(self, row):
        self.connection['public']['status'] = 'NEEDS_RECONNECT'


def setup_service(scopes=None):
    settings = Settings(_env_file=None, gmail_client_id='oauth-client', gmail_client_secret='client-secret',
        gmail_topic='projects/demo/topics/gmail', google_token_encryption_key=Fernet.generate_key().decode())
    repo = FakeRepository()
    google = MagicMock()
    scopes = frozenset({READ_SCOPE, SEND_SCOPE} if scopes is None else scopes)
    grant = Grant('new-access', PRINCIPAL.google_sub, PRINCIPAL.email, scopes, 'refresh-secret')
    google.inspect.return_value = google.refresh.return_value = grant
    google.watch.return_value = {'expiration': str(int((datetime.now(timezone.utc) + timedelta(days=7)).timestamp() * 1000)), 'historyId': '100'}
    service = GoogleConnectionService(settings, repo, google, TokenCipher(settings.google_token_encryption_key))
    return service, repo, google, settings


def client_for(service, settings, *, auth=True, base_url='https://api.example'):
    app = create_app(settings, mailbox_service=service)
    if auth:
        app.dependency_overrides[require_google_user] = lambda: PRINCIPAL
    return TestClient(app, base_url=base_url, raise_server_exceptions=False)


def connect(client, body=TOKENS, key='action-1'):
    return client.post('/api/v1/mailboxes/google/connect', json=body, headers={'Idempotency-Key': key})


@pytest.mark.parametrize('scopes,status,read,send', [
    ({READ_SCOPE, SEND_SCOPE}, 'CONNECTED', True, True),
    ({READ_SCOPE}, 'PARTIAL', True, False),
    ({SEND_SCOPE}, 'PARTIAL', False, True),
    (set(), 'PARTIAL', False, False),
])
def test_connection_scope_matrix(scopes, status, read, send):
    service, repo, google, settings = setup_service(scopes)
    response = connect(client_for(service, settings))
    assert response.status_code == 200
    data = response.json()['data']
    assert data['status'] == status
    assert data['capabilities'] == {'read': read, 'send': send}
    assert data['watch']['started'] is read
    assert len(repo.jobs) == int(read)
    assert google.refresh.call_count == 1
    assert 'refresh-secret' not in repr(repo.connection)
    assert 'access-secret' not in response.text
    assert service.cipher.decrypt(USER, PRINCIPAL.google_sub, repo.connection['encrypted_refresh_token']) == 'refresh-secret'


def test_not_connected_null_fields_and_no_provider_calls():
    service, _, google, settings = setup_service()
    response = client_for(service, settings).get('/api/v1/mailboxes/google/connection')
    assert response.json()['data'] == {'mailbox_ref': None, 'email': None, 'status': 'NOT_CONNECTED',
        'capabilities': {'read': False, 'send': False}, 'watch': {'started': False, 'expiration': None}}
    google.inspect.assert_not_called()


def test_refresh_absent_requires_reconnect_and_does_not_start_watch():
    service, repo, google, settings = setup_service()
    response = connect(client_for(service, settings), TOKENS | {'provider_refresh_token': None})
    assert response.json()['data']['status'] == 'NEEDS_RECONNECT'
    assert repo.connection['encrypted_refresh_token'] is None and not repo.jobs
    google.refresh.assert_not_called()
    google.watch.assert_not_called()


def test_revoked_refresh_is_reconnect_not_connected():
    service, repo, google, settings = setup_service()
    google.refresh.side_effect = GoogleRejected()
    response = connect(client_for(service, settings))
    assert response.json()['data']['status'] == 'NEEDS_RECONNECT'
    assert not repo.jobs


def test_same_key_exact_response_replay_without_oauth_or_job_repeat():
    service, repo, google, settings = setup_service()
    client = client_for(service, settings)
    first = connect(client)
    second = connect(client)
    assert first.json() == second.json()
    assert first.headers['x-request-id'] == second.headers['x-request-id'] == second.json()['meta']['request_id']
    assert google.refresh.call_count == 1 and len(repo.jobs) == 1


def test_key_reuse_different_token_returns_409():
    service, _, google, settings = setup_service()
    client = client_for(service, settings)
    connect(client)
    response = connect(client, TOKENS | {'provider_access_token': 'other-access'})
    assert response.status_code == 409 and response.json()['error']['code'] == 'IDEMPOTENCY_KEY_REUSED'
    assert google.refresh.call_count == 1


def test_in_progress_returns_409_without_google_calls():
    service, repo, google, settings = setup_service()
    repo.claim(USER, 'action-1', service.cipher.body_hash('access-secret', 'refresh-secret'))
    response = connect(client_for(service, settings))
    assert response.status_code == 409 and response.json()['error']['code'] == 'REQUEST_IN_PROGRESS'
    google.inspect.assert_not_called()


def test_transient_google_failure_can_retry_same_key():
    service, repo, google, settings = setup_service()
    client = client_for(service, settings)
    google.inspect.side_effect = ServiceError('GOOGLE_UNAVAILABLE', '잠시 후 다시 시도', 503)
    assert connect(client).status_code == 503
    assert not repo.records
    google.inspect.side_effect = None
    assert connect(client).status_code == 200


def test_lost_finish_ack_does_not_release_or_repeat_google():
    service, repo, google, settings = setup_service()
    client = client_for(service, settings)
    repo.finish = MagicMock(side_effect=ServiceError('MAILBOX_STORAGE_UNAVAILABLE', '저장 미확인', 503))
    assert connect(client).status_code == 503
    assert connect(client).json()['error']['code'] == 'REQUEST_IN_PROGRESS'
    assert google.refresh.call_count == 1


def test_watch_failure_queues_polling_fallback():
    service, repo, google, settings = setup_service()
    google.watch.side_effect = ServiceError('GOOGLE_UNAVAILABLE', 'watch 불가', 503)
    response = connect(client_for(service, settings))
    assert response.json()['data']['status'] == 'CONNECTED'
    assert response.json()['data']['watch'] == {'started': False, 'expiration': None}
    assert len(repo.jobs) == 1


def test_invalid_google_identity_cannot_overwrite_connection():
    service, repo, google, settings = setup_service()
    google.inspect.side_effect = GoogleRejected()
    response = connect(client_for(service, settings))
    assert response.status_code == 401 and repo.connection is None


@pytest.mark.parametrize('body', [TOKENS | {'user_id': USER}, TOKENS | {'provider_refresh_token': 123},
    TOKENS | {'provider_access_token': ''}, TOKENS | {'provider_refresh_token': ''}, {'provider_access_token': 'access-secret'}])
def test_invalid_body_does_not_echo_tokens(body):
    service, _, _, settings = setup_service()
    response = connect(client_for(service, settings), body)
    assert response.status_code == 422
    assert 'access-secret' not in response.text and 'refresh-secret' not in response.text


def test_missing_login_returns_401():
    service, _, _, settings = setup_service()
    assert connect(client_for(service, settings, auth=False)).status_code == 401


def test_missing_idempotency_key_never_calls_google():
    service, _, google, settings = setup_service()
    response = client_for(service, settings).post('/api/v1/mailboxes/google/connect', json=TOKENS)
    assert response.status_code == 422
    google.inspect.assert_not_called()


def test_status_get_does_not_refresh_or_renew_watch():
    service, _, google, settings = setup_service()
    client = client_for(service, settings)
    connect(client)
    google.reset_mock()
    response = client.get('/api/v1/mailboxes/google/connection')
    assert response.status_code == 200 and response.json()['data']['status'] == 'CONNECTED'
    google.refresh.assert_not_called()
    google.watch.assert_not_called()


def test_browser_refresh_uses_stored_connection_without_resending_google_tokens():
    service, repo, google, settings = setup_service()
    first_browser = client_for(service, settings)
    initial = connect(first_browser)
    assert initial.status_code == 200
    google.reset_mock()
    # A fresh browser session has the same verified Supabase user but no provider tokens.
    new_browser = client_for(service, settings)
    response = new_browser.get('/api/v1/mailboxes/google/connection')
    assert response.status_code == 200
    assert response.json()['data'] == initial.json()['data']
    google.inspect.assert_not_called()
    google.refresh.assert_not_called()
    assert len(repo.jobs) == 1


def test_browser_logout_does_not_delete_or_revoke_server_mailbox():
    service, repo, google, settings = setup_service()
    client = client_for(service, settings)
    connect(client)
    before = copy.deepcopy(repo.connection)
    google.reset_mock()
    signed_out = client_for(service, settings, auth=False)
    assert signed_out.get('/api/v1/mailboxes/google/connection').status_code == 401
    assert repo.connection == before
    google.assert_not_called()
    google.refresh.assert_not_called()
    signed_in_again = client_for(service, settings)
    assert signed_in_again.get('/api/v1/mailboxes/google/connection').json()['data']['status'] == 'CONNECTED'


def test_no_scopes_is_partial_and_cannot_read_or_send():
    service, repo, google, settings = setup_service(set())
    response = connect(client_for(service, settings))
    assert response.status_code == 200
    assert response.json()['data']['status'] == 'PARTIAL'
    for capability in ('read', 'send'):
        with pytest.raises(ServiceError, match='GMAIL_SCOPE_REQUIRED'):
            service.authorized(USER, capability)
    assert not repo.jobs
    google.watch.assert_not_called()


def test_expired_watch_is_shown_inactive_without_renewing_on_get():
    service, repo, google, settings = setup_service()
    client = client_for(service, settings)
    connect(client)
    repo.connection['public']['watch']['expiration'] = '2020-01-01T00:00:00Z'
    google.reset_mock()
    response = client.get('/api/v1/mailboxes/google/connection')
    assert response.json()['data']['watch']['started'] is False
    google.watch.assert_not_called()
    assert repo.connection['public']['watch']['started'] is True


def test_localhost_plain_http_requires_actual_loopback_peer():
    service, _, _, settings = setup_service()
    app = create_app(settings, mailbox_service=service)
    with TestClient(app, base_url='http://127.0.0.1:8000', client=('127.0.0.1', 50000)) as client:
        assert client.get('/api/v1/mailboxes/google/connection').status_code == 401


def test_stolen_host_header_does_not_enable_remote_http():
    service, _, _, settings = setup_service()
    app = create_app(settings, mailbox_service=service)
    client = TestClient(app, base_url='http://localhost:8000', client=('203.0.113.20', 50000))
    response = client.get('/api/v1/mailboxes/google/connection')
    assert response.status_code == 400


def test_insecure_remote_http_rejected_before_auth():
    service, _, google, settings = setup_service()
    client = client_for(service, settings, auth=False, base_url='http://api.example')
    response = connect(client)
    assert response.status_code == 400 and response.json()['error']['code'] == 'HTTPS_REQUIRED'
    google.inspect.assert_not_called()


def test_cors_allows_frontend_and_cache_control_header():
    service, _, _, settings = setup_service()
    response = client_for(service, settings).options('/api/v1/mailboxes/google/connect', headers={
        'Origin': 'http://localhost:3000', 'Access-Control-Request-Method': 'POST',
        'Access-Control-Request-Headers': 'authorization,idempotency-key,content-type,cache-control'})
    assert response.status_code == 200
    assert response.headers['access-control-allow-origin'] == 'http://localhost:3000'


def test_stored_scope_is_checked_before_refresh():
    service, _, google, settings = setup_service({SEND_SCOPE})
    connect(client_for(service, settings))
    google.refresh.reset_mock()
    with pytest.raises(ServiceError, match='GMAIL_SCOPE_REQUIRED'):
        service.authorized(USER, 'read')
    google.refresh.assert_not_called()


def test_revocation_on_use_updates_reconnect_state():
    service, repo, google, settings = setup_service()
    connect(client_for(service, settings))
    google.refresh.side_effect = GoogleRejected()
    with pytest.raises(ServiceError, match='GOOGLE_RECONNECT_REQUIRED'):
        service.authorized(USER, 'read')
    assert repo.connection['public']['status'] == 'NEEDS_RECONNECT'


def test_cipher_rejects_different_owner_or_subject():
    service, _, _, _ = setup_service()
    ciphertext = service.cipher.encrypt(USER, PRINCIPAL.google_sub, 'refresh-secret')
    for owner, subject in [('another-user', PRINCIPAL.google_sub), (USER, 'another-google-sub')]:
        with pytest.raises(ServiceError, match='CREDENTIALS_UNAVAILABLE'):
            service.cipher.decrypt(owner, subject, ciphertext)


def test_supabase_verified_identity_not_editable_user_metadata():
    settings = Settings(_env_file=None, supabase_url='https://example.supabase.co', supabase_publishable_key='publishable')
    calls = []
    def transport(request):
        calls.append(request.url.path)
        assert request.headers['Authorization'] == 'Bearer user-jwt'
        return httpx.Response(200, json={'id': USER, 'user_metadata': {'sub': 'attacker'},
            'identities': [{'provider': 'google', 'identity_data': {'sub': PRINCIPAL.google_sub, 'email': PRINCIPAL.email}}]})
    assert google_principal('user-jwt', settings, transport=httpx.MockTransport(transport)) == PRINCIPAL
    assert calls == ['/auth/v1/user']


@pytest.mark.parametrize('change', [{'aud': 'another-client'}, {'sub': 'another-user'}, {'expires_in': '0'}, {'email': 'another@example.com'}])
def test_tokeninfo_checks_audience_subject_expiration_and_email(change):
    _, _, _, settings = setup_service()
    data = {'aud': settings.gmail_client_id, 'sub': PRINCIPAL.google_sub, 'email': PRINCIPAL.email,
            'email_verified': 'true', 'expires_in': '3600', 'scope': READ_SCOPE} | change
    api = GoogleAPI(settings, transport=lambda *args: (200, data))
    with pytest.raises(GoogleRejected):
        api.inspect('access-secret', PRINCIPAL)


def test_refresh_revalidates_new_access_token_and_same_client():
    _, _, _, settings = setup_service()
    def transport(method, host, path, headers, body):
        if path == '/token':
            assert b'client_id=oauth-client' in body and b'grant_type=refresh_token' in body
            return 200, {'access_token': 'fresh', 'token_type': 'Bearer'}
        return 200, {'aud': 'another-client', 'expires_in': '3600'}
    with pytest.raises(GoogleRejected):
        GoogleAPI(settings, transport).refresh('refresh-secret', PRINCIPAL)


def test_provider_failure_does_not_leak_token_or_raw_error(caplog):
    _, _, _, settings = setup_service()
    def transport(*args):
        raise RuntimeError('provider_access_token=access-secret refresh-secret')
    with pytest.raises(ServiceError) as error:
        GoogleAPI(settings, transport).inspect('access-secret', PRINCIPAL)
    assert 'secret' not in str(error.value) and 'secret' not in caplog.text
