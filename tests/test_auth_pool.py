"""KAN-273 · KAN-268: 인증 HTTP 연결 풀을 재사용해도 인증 거부·격리 동작이 그대로인지. 외부 호출 없음(MockTransport)."""
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest import TestCase
from uuid import uuid4

import httpx
from fastapi.security import HTTPAuthorizationCredentials
from pydantic import SecretStr

from app.core.errors import ServiceError
from app.features.mailboxes.api.router import require_google_user
from app.features.mailboxes.application.auth import create_auth_client, google_principal

SETTINGS = SimpleNamespace(supabase_url='https://example.supabase.co',
                           supabase_publishable_key=SecretStr('publishable-key'))
TOKEN = 'secret-access-token-value'


def user(sub='g-sub', email='Buyer@Example.com', identities=None):
    if identities is None:
        identities = [{'provider': 'google', 'identity_data': {'sub': sub, 'email': email}}]
    return {'id': str(uuid4()), 'identities': identities}


class FakeAuth:
    """/auth/v1/user 대역. 요청 수와 연결별 요청을 센다."""

    def __init__(self, status=200, body=None, cookie=False):
        self.status, self.body, self.cookie = status, body, cookie
        self.requests = []
        self.lock = threading.Lock()

    def __call__(self, request):
        with self.lock:
            self.requests.append(request)
        headers = {'set-cookie': 'sb-session=leak; Path=/'} if self.cookie else {}
        if self.body is not None and not isinstance(self.body, dict):
            return httpx.Response(self.status, content=self.body, headers=headers)
        return httpx.Response(self.status, json=self.body if self.body is not None else user(), headers=headers)


def code_of(call):
    try:
        call()
    except ServiceError as error:
        return error.code, error.status
    raise AssertionError('ServiceError가 나지 않았다')


class SharedClientTests(TestCase):
    def test_shared_client_is_reused_and_left_open(self):
        auth = FakeAuth()
        with create_auth_client(transport=httpx.MockTransport(auth)) as client:
            first = google_principal(TOKEN, SETTINGS, client=client)
            second = google_principal(TOKEN, SETTINGS, client=client)
            self.assertFalse(client.is_closed)  # 요청마다 닫지 않는다
        self.assertEqual(len(auth.requests), 2)  # 원격 검증은 요청마다 그대로 한다
        self.assertEqual(first.email, 'buyer@example.com')
        self.assertEqual(second.google_sub, 'g-sub')
        request = auth.requests[0]
        self.assertEqual(request.url.path, '/auth/v1/user')
        self.assertEqual(request.headers['authorization'], 'Bearer ' + TOKEN)
        self.assertEqual(request.headers['apikey'], 'publishable-key')

    def test_without_shared_client_temporary_client_still_works(self):
        auth = FakeAuth()
        google_principal(TOKEN, SETTINGS, transport=httpx.MockTransport(auth))
        self.assertEqual(len(auth.requests), 1)

    def test_concurrent_calls_on_shared_client(self):
        auth = FakeAuth()
        with create_auth_client(transport=httpx.MockTransport(auth)) as client:
            with ThreadPoolExecutor(max_workers=8) as pool:
                results = list(pool.map(lambda _: google_principal(TOKEN, SETTINGS, client=client), range(32)))
        self.assertEqual(len(results), 32)
        self.assertEqual(len(auth.requests), 32)

    def test_auth_cookies_are_not_kept_on_shared_client(self):
        # 공유 클라이언트에 쿠키가 남으면 다음 사용자 요청에 섞인다
        auth = FakeAuth(cookie=True)
        with create_auth_client(transport=httpx.MockTransport(auth)) as client:
            google_principal(TOKEN, SETTINGS, client=client)
            google_principal('other-user-token', SETTINGS, client=client)
            self.assertEqual(len(client.cookies), 0)
        self.assertNotIn('cookie', auth.requests[1].headers)
        self.assertEqual(auth.requests[1].headers['authorization'], 'Bearer other-user-token')


class RejectionTests(TestCase):
    def check(self, auth, expected, token=TOKEN):
        with create_auth_client(transport=httpx.MockTransport(auth)) as client:
            self.assertEqual(code_of(lambda: google_principal(token, SETTINGS, client=client)), expected)

    def test_expired_or_forged_token(self):
        for status in (401, 403):
            with self.subTest(status=status):
                self.check(FakeAuth(status=status, body={}), ('AUTH_REQUIRED', 401))

    def test_missing_or_malformed_token_makes_no_request(self):
        for token in ('', None, 'two words', 'tab\tinside'):
            with self.subTest(token=token):
                auth = FakeAuth()
                self.check(auth, ('AUTH_REQUIRED', 401), token=token)
                self.assertEqual(auth.requests, [])

    def test_google_identity_mismatch(self):
        cases = {
            'no google': user(identities=[{'provider': 'email', 'identity_data': {}}]),
            'two google': user(identities=[{'provider': 'google', 'identity_data': {'sub': 'a', 'email': 'a@x.co'}}] * 2),
            'no email': user(email=None),
            'bad email': user(email='not-an-email'),
            'no subject': user(sub=''),
        }
        for name, body in cases.items():
            with self.subTest(name):
                self.check(FakeAuth(body=body), ('GOOGLE_RECONNECT_REQUIRED', 401))

    def test_auth_upstream_failures(self):
        cases = {'5xx': FakeAuth(status=500, body={}), 'not json': FakeAuth(body=b'<html>'),
                 'bad user id': FakeAuth(body={'id': 'not-a-uuid', 'identities': []})}
        for name, auth in cases.items():
            with self.subTest(name):
                self.check(auth, ('AUTH_UNAVAILABLE', 503))

    def test_network_error_is_unavailable(self):
        def fail(request):
            raise httpx.ConnectError('down', request=request)
        self.check(fail, ('AUTH_UNAVAILABLE', 503))

    def test_missing_configuration(self):
        settings = SimpleNamespace(supabase_url=None, supabase_publishable_key=None)
        self.assertEqual(code_of(lambda: google_principal(TOKEN, settings)), ('CONFIGURATION_REQUIRED', 503))

    def test_token_is_not_logged_on_failure(self):
        with create_auth_client(transport=httpx.MockTransport(FakeAuth(status=502, body={}))) as client:
            with self.assertLogs('gyeonjwo', level='WARNING') as logs:
                code_of(lambda: google_principal(TOKEN, SETTINGS, client=client))
        self.assertNotIn(TOKEN, '\n'.join(logs.output))
        self.assertNotIn('publishable-key', '\n'.join(logs.output))


class DependencyTests(TestCase):
    """require_google_user가 앱 수명의 공유 클라이언트를 쓰고, 허용 목록 거부를 그대로 전달하는지."""

    def request(self, auth, checker):
        client = create_auth_client(transport=httpx.MockTransport(auth))
        self.addCleanup(client.close)
        state = SimpleNamespace(settings=SETTINGS, auth_http_client=client, service_lock=threading.Lock(),
                                membership_checker=checker)
        return SimpleNamespace(app=SimpleNamespace(state=state))

    def credentials(self):
        return HTTPAuthorizationCredentials(scheme='Bearer', credentials=TOKEN)

    def test_uses_app_client_and_checks_membership(self):
        auth, seen = FakeAuth(), []
        checker = SimpleNamespace(require=seen.append)
        principal = require_google_user(self.request(auth, checker), self.credentials(), None)
        self.assertEqual(len(auth.requests), 1)
        self.assertEqual(seen, [principal])

    def test_membership_denial_is_propagated(self):
        def deny(principal):
            raise ServiceError('ACCESS_DENIED', '접근 권한이 없습니다.', 403)
        request = self.request(FakeAuth(), SimpleNamespace(require=deny))
        self.assertEqual(code_of(lambda: require_google_user(request, self.credentials(), None)), ('ACCESS_DENIED', 403))

    def test_missing_credentials_makes_no_request(self):
        auth = FakeAuth()
        request = self.request(auth, SimpleNamespace(require=lambda p: None))
        self.assertEqual(code_of(lambda: require_google_user(request, None, None)), ('AUTH_REQUIRED', 401))
        self.assertEqual(auth.requests, [])


if __name__ == '__main__':
    import unittest
    unittest.main()
