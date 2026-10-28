import httpx
import pytest

from app.features.auth.application.auth import AccessDenied, verify_member
from app.core.config import Settings


def settings():
    return Settings(_env_file=None, supabase_url='https://example.supabase.co',
                    supabase_publishable_key='publishable-test', supabase_secret_key='server-secret')


@pytest.mark.parametrize('active', [True, False, 'true'])
def test_user_context_membership(active):
    def handler(request):
        assert request.headers['apikey'] == 'publishable-test'
        assert request.headers['authorization'] == 'Bearer user-jwt'
        assert 'server-secret' not in str(request.headers)
        if request.url.path == '/auth/v1/user':
            return httpx.Response(200, json={'id': 'user-id'})
        assert request.url.path == '/rest/v1/rpc/check_app_access'
        return httpx.Response(200, json=active)
    if active is True:
        assert verify_member('user-jwt', settings(), transport=httpx.MockTransport(handler)) == 'user-id'
    else:
        with pytest.raises(AccessDenied):
            verify_member('user-jwt', settings(), transport=httpx.MockTransport(handler))


def test_invalid_user_does_not_call_membership():
    calls = []
    def handler(request):
        calls.append(request.url.path)
        return httpx.Response(401, json={'message': 'expired'})
    with pytest.raises(AccessDenied):
        verify_member('expired', settings(), transport=httpx.MockTransport(handler))
    assert calls == ['/auth/v1/user']
