"""Pure request identity helpers, not an in-memory substitute for persistent idempotency.

The future DB adapter must atomically claim (user_id, method, path, key), compare
body_hash, and store the first response together with the business mutation/job.
No record expiry or automatic takeover of in-progress requests is assumed here.
"""
import hashlib
import json
from dataclasses import dataclass
from typing import Any

from app.core.errors import ServiceError


@dataclass(frozen=True)
class RequestIdentity:
    user_id: str
    method: str
    path: str
    key: str
    body_hash: str


def request_identity(user_id: str, method: str, path: str, key: str | None, body: Any) -> RequestIdentity:
    if not key or not key.strip():
        raise ServiceError('INVALID_REQUEST', 'Idempotency-Key가 필요합니다.', 422,
                           fields=['header.Idempotency-Key'], retryable=False)
    if not user_id or not path.startswith('/') or method.upper() not in {'POST', 'PUT', 'PATCH', 'DELETE'}:
        raise ValueError('An authenticated user and mutation route are required')
    # Inputs must already be parsed JSON. Preserve types and array order; object key order is irrelevant.
    canonical = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)
    return RequestIdentity(user_id, method.upper(), path, key,
                           hashlib.sha256(canonical.encode('utf-8')).hexdigest())


def require_same_body(identity: RequestIdentity, stored_body_hash: str) -> None:
    if identity.body_hash != stored_body_hash:
        raise ServiceError('IDEMPOTENCY_KEY_REUSED', '같은 키에 다른 요청 본문을 사용할 수 없습니다.', 409,
                           retryable=False)


def request_in_progress() -> ServiceError:
    return ServiceError('REQUEST_IN_PROGRESS', '같은 요청이 처리 중입니다. 같은 키로 확인해 주세요.', 409,
                        retryable=False)
