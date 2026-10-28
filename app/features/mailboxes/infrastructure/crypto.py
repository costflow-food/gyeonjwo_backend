import base64
import hashlib
import hmac
import json

from cryptography.fernet import Fernet, InvalidToken

from app.core.errors import ServiceError


class TokenCipher:
    """Server-side encryption; the key is kept outside Supabase, never generated at startup."""
    def __init__(self, secret):
        try:
            key = secret.get_secret_value().encode('ascii')
            self.cipher = Fernet(key)
            self.hash_key = hmac.digest(base64.urlsafe_b64decode(key), b'google-connect-request-v1', 'sha256')
        except Exception:
            raise ServiceError('CONFIGURATION_REQUIRED', '토큰 암호화 설정이 필요합니다.', 503) from None

    def encrypt(self, owner_id, google_sub, refresh_token):
        payload = json.dumps({'owner_id': owner_id, 'google_sub': google_sub, 'refresh_token': refresh_token})
        return self.cipher.encrypt(payload.encode()).decode('ascii')

    def decrypt(self, owner_id, google_sub, ciphertext):
        try:
            payload = json.loads(self.cipher.decrypt(ciphertext.encode()))
            if payload['owner_id'] != owner_id or payload['google_sub'] != google_sub:
                raise ValueError('Owner mismatch')
            return payload['refresh_token']
        except (InvalidToken, ValueError, KeyError, TypeError, AttributeError):
            raise ServiceError('CREDENTIALS_UNAVAILABLE', '저장된 연결 정보를 확인할 수 없습니다.', 503) from None

    def body_hash(self, access_token, refresh_token):
        raw = json.dumps([access_token, refresh_token], separators=(',', ':'), ensure_ascii=False).encode()
        return hmac.new(self.hash_key, raw, hashlib.sha256).hexdigest()
