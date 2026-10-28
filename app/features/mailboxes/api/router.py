import ipaddress
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials

from app.api.dependencies import bearer
from app.api.responses import Envelope, api_json
from app.core.errors import ServiceError
from app.features.mailboxes.application.auth import google_principal
from app.features.mailboxes.application.service import GoogleConnectionService
from app.features.mailboxes.domain.schemas import GoogleConnection, GoogleConnectRequest
from app.features.mailboxes.infrastructure.crypto import TokenCipher
from app.features.mailboxes.infrastructure.google import GoogleAPI
from app.features.mailboxes.infrastructure.repository import MailboxRepository
from app.shared.infrastructure.db.client import make_server_client

router = APIRouter(prefix='/api/v1/mailboxes/google', tags=['Google mailbox'])


def make_mailbox_service(settings):
    settings.require_google_connect()
    return GoogleConnectionService(settings, MailboxRepository(make_server_client(settings)),
                                   GoogleAPI(settings), TokenCipher(settings.google_token_encryption_key))


def require_secure_transport(request: Request):
    if request.url.scheme == 'https':
        return
    try:
        loopback = ipaddress.ip_address(request.client.host).is_loopback
    except (ValueError, AttributeError):
        loopback = False
    if not loopback or request.url.hostname not in {'localhost', '127.0.0.1', '::1'}:
        raise ServiceError('HTTPS_REQUIRED', '계정 연결에는 HTTPS 주소가 필요합니다.', 400, retryable=False)


def require_google_user(request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    secure=Depends(require_secure_transport)):
    if not credentials:
        raise ServiceError('AUTH_REQUIRED', '로그인이 필요합니다.', 401)
    return google_principal(credentials.credentials, request.app.state.settings)


def get_mailbox_service(request: Request):
    with request.app.state.service_lock:
        if request.app.state.mailbox_service is None:
            settings = request.app.state.settings
            try:
                request.app.state.mailbox_service = make_mailbox_service(settings)
            except RuntimeError:
                raise ServiceError('CONFIGURATION_REQUIRED', 'Google 연결·DB 저장 설정이 필요합니다.', 503) from None
        return request.app.state.mailbox_service


@router.post('/connect', response_model=Envelope[GoogleConnection])
def connect(payload: GoogleConnectRequest, request: Request,
    principal=Depends(require_google_user), service=Depends(get_mailbox_service),
    idempotency_key: Annotated[str | None, Header(alias='Idempotency-Key')] = None):
    result = service.connect(principal, payload, idempotency_key)
    worker = getattr(request.app.state, 'connected_mailbox_worker', None)
    if worker:
        worker.wake.set()
    return JSONResponse(result.body, status_code=result.status,
                        headers={'X-Request-ID': result.body['meta']['request_id']})


@router.get('/connection', response_model=Envelope[GoogleConnection])
def connection(principal=Depends(require_google_user), service=Depends(get_mailbox_service)):
    return api_json(service.connection(principal).model_dump(mode='json'))
