"""Resolve Google identity from Supabase Auth's verified user response.

Do not use request user IDs, decoded-but-unverified JWTs or editable user_metadata.
Newly signed-up users do not need the legacy check_app_access RPC for these routes.
"""
from uuid import UUID

import httpx

from app.core.errors import ServiceError
from app.features.mailboxes.domain.schemas import GooglePrincipal


def google_principal(token, settings, *, transport=None):
    if not settings.supabase_url or not settings.supabase_publishable_key:
        raise ServiceError('CONFIGURATION_REQUIRED', '사용자 인증 설정이 필요합니다.', 503)
    if not token or any(char.isspace() for char in token):
        raise ServiceError('AUTH_REQUIRED', '다시 로그인해 주세요.', 401)
    try:
        with httpx.Client(timeout=15, transport=transport, follow_redirects=False) as client:
            response = client.get(str(settings.supabase_url).rstrip('/') + '/auth/v1/user', headers={
                'apikey': settings.supabase_publishable_key.get_secret_value(), 'Authorization': 'Bearer ' + token})
        if response.status_code in (401, 403):
            raise ServiceError('AUTH_REQUIRED', '다시 로그인해 주세요.', 401)
        if response.status_code != 200:
            raise ServiceError('AUTH_UNAVAILABLE', '인증 서비스에 연결할 수 없습니다.', 503)
        user = response.json()
        user_id = str(UUID(user['id']))
        identities = [row for row in user.get('identities', []) if row.get('provider') == 'google']
        if len(identities) != 1:
            raise ServiceError('GOOGLE_RECONNECT_REQUIRED', 'Google 계정으로 다시 로그인해 주세요.', 401)
        data = identities[0].get('identity_data') or {}
        subject, email = data.get('sub'), data.get('email')
        if not isinstance(subject, str) or not subject or not isinstance(email, str) or '@' not in email:
            raise ServiceError('GOOGLE_RECONNECT_REQUIRED', 'Google 계정 연결 정보를 확인해 주세요.', 401)
        return GooglePrincipal(user_id, subject, email.casefold())
    except ServiceError:
        raise
    except (httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError):
        raise ServiceError('AUTH_UNAVAILABLE', '인증 응답을 확인할 수 없습니다.', 503) from None
