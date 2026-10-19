from collections import OrderedDict, deque
from dataclasses import dataclass
from threading import Lock
from time import monotonic

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .errors import AppError

bearer = HTTPBearer(auto_error=False)


@dataclass(frozen=True)
class Principal:
    user_id: str
    reviewer: bool


def authorize(user: dict, settings) -> Principal:
    user_id = user.get("id")
    if not user_id or user_id not in settings.users | settings.reviewers:
        raise AppError(403, "not_allowed", "이 팀의 CostFlow 접근 권한이 없습니다.")
    return Principal(user_id, user_id in settings.reviewers)


def require_user(
    request: Request, credentials: HTTPAuthorizationCredentials | None = Depends(bearer)
) -> Principal:
    if credentials is None:
        raise AppError(401, "authentication_required", "로그인이 필요합니다.")
    settings = request.app.state.settings
    if not settings.users and not settings.reviewers:
        raise AppError(503, "access_not_configured", "서버의 팀 사용자 허용 목록을 설정해 주세요.")
    return authorize(request.app.state.gateway.user(credentials.credentials), settings)


def require_reviewer(user: Principal = Depends(require_user)) -> Principal:
    if not user.reviewer:
        raise AppError(403, "reviewer_required", "최종 결정·거절에는 검토자 권한이 필요합니다.")
    return user


class LoginLimiter:
    """Per-process protection; production still needs a proxy/shared rate limiter."""

    def __init__(self, attempts=10, window=60):
        self.attempts, self.window = attempts, window
        self.entries = OrderedDict()
        self.lock = Lock()

    def check(self, address: str):
        now = monotonic()
        with self.lock:
            attempts = self.entries.setdefault(address, deque())
            while attempts and attempts[0] <= now - self.window:
                attempts.popleft()
            self.entries.move_to_end(address)
            if len(attempts) >= self.attempts:
                raise AppError(429, "rate_limited", "잠시 후 다시 로그인해 주세요.")
            attempts.append(now)
            if len(self.entries) > 10000:
                self.entries.popitem(last=False)
