"""DEV 존 로그인 생략(DEV_NO_AUTH_USER_ID): DEV에서만 켜지고 운영에서는 서버가 시작하지 않는다."""
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch
from uuid import UUID

from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.core.errors import ServiceError
from app.features.mailboxes.api.router import require_google_user
from app.features.system.application.diagnostics import require_startup_configuration

DEV_USER = '11111111-2222-4333-8444-555555555555'
DEV_DB = 'https://mumzaoohsmdiegtmjhha.supabase.co'
PRD_DB = 'https://qlabhitzpdiawtbdmlgw.supabase.co'


def settings(**values):
    base = {'supabase_url': DEV_DB, 'supabase_secret_key': 'sb_secret_x',
            'supabase_publishable_key': 'sb_publishable_x', 'app_environment': 'development'}
    base.update(values)
    return Settings(_env_file=None, **base)


class DevNoAuthStartupTests(TestCase):
    def test_off_by_default(self):
        self.assertFalse(settings().dev_no_auth)
        self.assertFalse(settings(dev_no_auth_user_id='').dev_no_auth)

    def test_allowed_on_dev_database_in_development(self):
        value = settings(dev_no_auth_user_id=DEV_USER)
        self.assertTrue(value.dev_no_auth)
        require_startup_configuration(value)  # 시작 허용

    def test_refused_in_production_environment(self):
        with self.assertRaises(SystemExit) as raised:
            require_startup_configuration(settings(dev_no_auth_user_id=DEV_USER, app_environment='production'))
        self.assertEqual(str(raised.exception), 'DEV_NO_AUTH_USER_ID')

    def test_refused_on_production_database_even_in_development(self):
        with self.assertRaises(SystemExit):
            require_startup_configuration(settings(dev_no_auth_user_id=DEV_USER, supabase_url=PRD_DB))

    def test_invalid_user_id_is_rejected(self):
        with self.assertRaises(Exception):
            settings(dev_no_auth_user_id='not-a-uuid')


class DevNoAuthRequestTests(TestCase):
    def client(self, value):
        app = FastAPI()
        app.state.settings = value
        app.state.auth_http_client = Mock()
        app.state.service_lock = Mock(__enter__=Mock(), __exit__=Mock(return_value=False))

        @app.get('/whoami')
        def whoami(principal=Depends(require_google_user)):
            return {'user_id': principal.user_id, 'email': principal.email}

        @app.exception_handler(ServiceError)
        def handle(request, error):
            from fastapi.responses import JSONResponse
            return JSONResponse({'code': error.code}, status_code=error.status)

        return TestClient(app, base_url='https://testserver'), app

    def test_dev_no_auth_returns_fixed_user_without_token_or_member_check(self):
        client, app = self.client(settings(dev_no_auth_user_id=DEV_USER, dev_no_auth_email=' Dev@Thoner.Local '))
        with patch('app.features.mailboxes.api.router.google_principal') as verify, \
                patch('app.features.mailboxes.api.router.get_membership_checker') as members:
            response = client.get('/whoami')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {'user_id': str(UUID(DEV_USER)), 'email': 'dev@thoner.local'})
        verify.assert_not_called()
        members.assert_not_called()
        app.state.auth_http_client.get.assert_not_called()

    def test_without_dev_no_auth_token_is_still_required(self):
        client, _ = self.client(settings())
        self.assertEqual(client.get('/whoami').json(), {'code': 'AUTH_REQUIRED'})


class DevReadyTests(TestCase):
    def test_dev_ready_checks_database_only_and_lists_disabled_features(self):
        from app.features.system.api import router as system
        probe = SimpleNamespace(readiness=lambda: ('ready', True))
        request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
            settings=settings(dev_no_auth_user_id=DEV_USER, app_build_revision='abc1234'))))
        with patch.object(system, '_readiness_probes', return_value=(None, probe)):
            body = system.ready(request)
        self.assertEqual(body['status'], 'ready')
        self.assertEqual(body['degraded_features'], [])
        self.assertEqual(body['disabled_features'], ['google_oauth', 'llm'])
        self.assertEqual(body['build_revision'], 'abc1234')

    def test_dev_ready_reports_database_failure(self):
        from app.features.system.api import router as system
        probe = SimpleNamespace(readiness=lambda: ('failed', None))
        request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
            settings=settings(dev_no_auth_user_id=DEV_USER))))
        with patch.object(system, '_readiness_probes', return_value=(None, probe)):
            response = system.ready(request)
        self.assertEqual(response.status_code, 503)
