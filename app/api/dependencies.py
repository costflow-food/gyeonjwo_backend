from typing import Annotated
from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from app.features.auth.application.auth import AccessDenied, AuthenticationFailed, AuthUnavailable, verify_member
from app.shared.infrastructure.db.client import make_server_client
from app.core.errors import ServiceError
from app.features.review.application.services import Services

bearer = HTTPBearer(auto_error=False)

def get_service(request: Request):
    with request.app.state.service_lock:
        if request.app.state.services is None:
            try:
                request.app.state.services = Services(request.app.state.settings, make_server_client(request.app.state.settings))
            except (RuntimeError, ValueError):
                raise ServiceError('CONFIGURATION_REQUIRED', '서버 연결 설정이 필요합니다.', 503) from None
    return request.app.state.services

def require_member(request: Request, credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)]):
    if not credentials or credentials.scheme.lower() != 'bearer':
        raise ServiceError('AUTH_REQUIRED', '사용자 로그인이 필요합니다.', 401)
    try:
        return verify_member(credentials.credentials, request.app.state.settings)
    except AuthenticationFailed:
        raise ServiceError('AUTH_REQUIRED', '로그인 토큰이 유효하지 않습니다.', 401) from None
    except AccessDenied:
        raise ServiceError('ACCESS_DENIED', '로그인 또는 프로젝트 접근 권한을 확인해 주세요.', 403) from None
    except AuthUnavailable:
        raise ServiceError('AUTH_UNAVAILABLE', '인증 서비스에 연결할 수 없습니다.', 503) from None
    except RuntimeError:
        raise ServiceError('CONFIGURATION_REQUIRED', '사용자 인증 설정이 필요합니다.', 503) from None
