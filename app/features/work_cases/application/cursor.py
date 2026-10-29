"""Signed, versioned keyset cursors scoped to owner and normalized list filters."""
import base64
import hashlib
import hmac
import json
from datetime import datetime, timezone
from uuid import UUID

from app.core.errors import ServiceError


def utc_time(raw: str) -> datetime:
    value = datetime.fromisoformat(raw.replace('Z', '+00:00'))
    if value.tzinfo is None:
        raise ValueError('Timezone required')
    return value.astimezone(timezone.utc)


def iso_z(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec='microseconds').replace('+00:00', 'Z')


class CursorCodec:
    def __init__(self, key: str):
        self.key = hashlib.sha256(b'gyeonjwo:work-case-list:v1\0' + key.encode()).digest()

    def scope(self, owner, status, archived, query):
        return hashlib.sha256(json.dumps([str(owner), status, archived, query],
                                        ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()

    def encode(self, scope, snapshot, after):
        payload = json.dumps({'v': 1, 'scope': scope, 'snapshot': snapshot,
                              'after': list(after)}, separators=(',', ':')).encode()
        signature = hmac.digest(self.key, payload, 'sha256')
        return base64.urlsafe_b64encode(payload + signature).decode().rstrip('=')

    def decode(self, token, scope):
        try:
            if len(token) > 2048:
                raise ValueError('Cursor too long')
            packed = base64.b64decode(token + '=' * (-len(token) % 4), altchars=b'-_', validate=True)
            payload, signature = packed[:-32], packed[-32:]
            if not hmac.compare_digest(signature, hmac.digest(self.key, payload, 'sha256')):
                raise ValueError('Invalid signature')
            data = json.loads(payload)
            if data['v'] != 1 or data['scope'] != scope:
                raise ValueError('Cursor scope changed')
            snapshot = iso_z(utc_time(data['snapshot']))
            after_time, after_id = data['after']
            return snapshot, (iso_z(utc_time(after_time)), str(UUID(after_id)))
        except (ValueError, TypeError, KeyError, IndexError, UnicodeError):
            raise ServiceError('INVALID_CURSOR', '검색·필터를 변경했다면 첫 페이지부터 조회해 주세요.',
                               422, fields=['query.cursor'], retryable=False) from None
