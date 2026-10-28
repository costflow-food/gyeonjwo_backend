"""Verify user identity and membership in the USER JWT context, never service_role."""
import httpx

from app.core.config import Settings


class AccessDenied(RuntimeError):
    pass


class AuthenticationFailed(AccessDenied):
    pass


class AuthUnavailable(RuntimeError):
    pass


def verify_member(token: str, settings: Settings, *, transport=None) -> str:
    key = settings.supabase_publishable_key
    if not settings.supabase_url or not key or not key.get_secret_value():
        raise RuntimeError('Supabase user authentication is not configured')
    if not token or any(character.isspace() for character in token):
        raise AuthenticationFailed('Invalid access token')
    headers = {'apikey': key.get_secret_value(), 'Authorization': f'Bearer {token}'}
    base = str(settings.supabase_url).rstrip('/')
    try:
        with httpx.Client(timeout=15, transport=transport, headers=headers) as client:
            user = client.get(f'{base}/auth/v1/user')
            if user.status_code != 200:
                if user.status_code >= 500:
                    raise AuthUnavailable('Authentication service unavailable')
                raise AuthenticationFailed('User authentication failed')
            user_data = user.json()
            user_id = user_data.get('id') if isinstance(user_data, dict) else None
            if not isinstance(user_id, str) or not user_id.strip():
                raise AuthenticationFailed('User authentication failed')
            allowed = client.post(f'{base}/rest/v1/rpc/check_app_access', json={})
            if allowed.status_code >= 500:
                raise AuthUnavailable('Membership service unavailable')
            if allowed.status_code == 401:
                raise AuthenticationFailed('User token expired')
            if allowed.status_code != 200 or allowed.json() is not True:
                raise AccessDenied('User is not an active app member')
            return user_id
    except httpx.HTTPError:
        raise AuthUnavailable('Unable to verify application access') from None
    except ValueError:
        raise AuthenticationFailed('Invalid authentication response') from None
