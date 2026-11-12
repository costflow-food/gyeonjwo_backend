"""Application assembly. Feature endpoints live under app/features/*/api."""
import asyncio
import hashlib
import logging
import math
import threading
import time
import httpx
from contextlib import asynccontextmanager
from anyio import move_on_after, to_thread
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from starlette.exceptions import HTTPException as StarletteHTTPException
from app.api.responses import ErrorEnvelope, api_error, service_error_response
from app.core.config import get_settings
from app.core.logs import configure_logging, error_origin, stack_summary
from app.core.request_context import current_request_id, new_request_id
from app.shared.infrastructure.db.contracts import OwnershipLost
from app.core.errors import ServiceError
from app.features.mail.infrastructure.gmail import verify_push
from app.core.http_limits import BodyLimitMiddleware
from app.features.system.api.router import router as system_router
from app.features.mail.api.router import router as mail_router
from app.features.mailboxes.api.router import router as mailbox_router, make_mailbox_service
from app.features.mailboxes.application.worker import ConnectedMailboxWorker
from app.features.work_cases.api.router import get_list_service, router as work_cases_router
from app.features.work_cases.api.revisions import router as work_revisions_router
from app.features.workflow_logs.router import router as workflow_logs_router
from app.features.suppliers.router import router as suppliers_router
from app.features.work_cases.api.mail_drafts import router as mail_drafts_router
from app.features.work_cases.api.candidates import router as candidates_router
from app.features.work_cases.api.quotes import router as quotes_router
from app.features.work_cases.api.review_card import router as review_card_router
from app.features.mail_management.api.router import router as mail_management_router
from app.features.system.application.diagnostics import require_startup_configuration
from app.features.system.application.workers import (
    create_missing_rfq_workers, retry_unavailable_workers, start_call_recording_worker, start_graph_worker,
    start_transcription_worker)
from app.features.call_recordings.router import router as call_recordings_router

logger = logging.getLogger('gyeonjwo')
RATE_WINDOW_SECONDS = 60
# 로그인한 요청은 토큰(사용자)마다, 토큰 없는 요청은 IP마다 이만큼. 같은 사무실(같은 IP)의 여러 사용자가 화면 폴링
# (목록 15초, 진행 상황·초안 3초)을 합산당해 429가 나지 않게 사용자 단위로 센다
RATE_LIMIT_PER_WINDOW = 120
# 토큰을 바꿔 가며 사용자 단위 제한을 피하지 못하게 IP 전체에도 넉넉한 상한을 둔다
RATE_LIMIT_PER_IP_WINDOW = 600
RATE_LIMIT_EXEMPT_PATHS = frozenset({'/api/v1/webhooks/gmail'})

async def prime_work_case_reads(state):
    service = getattr(state, 'work_case_list_service', None)
    if service is None:
        return

    def connect():
        client = service.repo.client
        # Warm the list's own connection without reading any user's rows.
        try:
            postgrest = client.postgrest
            postgrest.session.head(
                str(client.rest_url).rstrip('/') + '/work_cases',
                headers=postgrest.headers, params={'select': 'id', 'limit': 0}, timeout=0.2)
        except httpx.HTTPError:
            pass

    # Startup and API availability must not depend on the optional probe.
    with move_on_after(0.2):
        await to_thread.run_sync(connect, abandon_on_cancel=True)


def bearer_token(header: str | None) -> str | None:
    """Authorization: Bearer <토큰>의 토큰. 없거나 형식이 다르면 None."""
    if not header:
        return None
    scheme, _, token = header.partition(' ')
    token = token.strip()
    return token if scheme.lower() == 'bearer' and token else None


def route_template(request) -> str:
    """요청 경로의 템플릿(/api/v1/work-cases/{case_id}). 실제 ID가 든 경로는 남기지 않는다."""
    return getattr(request.scope.get('route'), 'path', 'unknown')


def log_unexpected(request, error: BaseException, request_id: str) -> None:
    """처리하지 못한 예외를 종류·코드 위치·DB 오류 코드와 함께 남긴다(메시지는 남기지 않는다)."""
    logger.error('request failed status=503 code=UNHANDLED_EXCEPTION path_template=%s request_id=%s exception=%s %s stack=%s',
                 route_template(request), request_id, type(error).__name__, error_origin(error), stack_summary(error))


def create_app(settings=None, push_verifier=verify_push, mailbox_service=None):
    settings = settings or get_settings()
    configure_logging()
    require_startup_configuration(settings)

    @asynccontextmanager
    async def lifespan(app):
        connected_worker = None
        retry_task = None
        # Workers start by default; a missing secret only keeps that worker off (names were logged at startup).
        try:
            settings.require_google_connect()
            settings.require_classification()
            if app.state.mailbox_service is None:
                app.state.mailbox_service = make_mailbox_service(settings)
            connected_worker = ConnectedMailboxWorker(app.state.mailbox_service)
            app.state.connected_mailbox_worker = connected_worker
        except RuntimeError:
            logger.warning('mailbox worker unavailable code=CONFIGURATION_REQUIRED; API remains available')
        try:
            rfq_workers = create_missing_rfq_workers(app.state, settings)
            start_graph_worker(app.state, settings)
            start_call_recording_worker(app.state, settings)
            start_transcription_worker(app.state, settings)
            await prime_work_case_reads(app.state)
            for prepared_worker in (connected_worker, *rfq_workers):
                if prepared_worker is not None:
                    prepared_worker.start()
            # 부팅 순간 DB 계약이 어긋나 있었어도, 고쳐지면 재배포 없이 워커가 올라온다.
            retry_task = asyncio.create_task(retry_unavailable_workers(app.state, settings))
            yield
        finally:
            if retry_task is not None:
                retry_task.cancel()
                try:
                    await retry_task
                except asyncio.CancelledError:
                    pass
            # 재시도로 나중에 뜬 워커도 닫히도록 state에서 읽는다.
            for name in ('rfq_workflow_worker', 'rfq_metadata_worker', 'connected_mailbox_worker', 'extraction_worker',
                         'call_recording_worker', 'transcription_worker'):
                worker = getattr(app.state, name, None)
                if worker:
                    worker.close()

    # Explicitly override FastAPI's default HTTPValidationError schema: runtime uses ErrorEnvelope.
    common_errors = {status: {'model': ErrorEnvelope} for status in
                     (400, 401, 403, 404, 405, 409, 413, 422, 429, 502, 503, 504)}
    # 운영에서는 API 문서(/docs·/redoc·/openapi.json)를 열지 않는다
    docs = {} if settings.app_environment != 'production' else {'docs_url': None, 'redoc_url': None, 'openapi_url': None}
    app = FastAPI(title='견줘 Python Backend', version='1.0.0', lifespan=lifespan,
                  responses=common_errors, **docs)
    app.state.settings = settings
    app.state.mailbox_service = mailbox_service
    app.state.membership_checker = None
    app.state.connected_mailbox_worker = None
    app.state.extraction_worker = None
    app.state.graph_startup_code = 'GRAPH_RUNTIME_UNAVAILABLE'
    app.state.rfq_metadata_worker = None
    app.state.rfq_workflow_worker = None
    app.state.call_recording_worker = None
    app.state.transcription_worker = None
    app.state.call_recording_service = None
    app.state.service_lock = threading.Lock()
    if getattr(settings, 'google_token_encryption_key', None):
        # Build the list clients before accepting requests. Connections remain lazy.
        get_list_service(app.state)
    app.state.push_verifier = push_verifier
    app.add_middleware(BodyLimitMiddleware)
    rate_lock, rates = threading.Lock(), {}

    @app.middleware('http')
    async def headers_and_limits(request, call_next):
        request_id = new_request_id()
        now = time.monotonic()
        response = None
        # Google Pub/Sub pushes are authenticated separately; throttling them would only trigger redelivery storms.
        if request.url.path not in RATE_LIMIT_EXEMPT_PATHS:
            host = request.client.host if request.client else 'unknown'
            token = bearer_token(request.headers.get('authorization'))
            # 토큰은 이 단계에서 검증하지 않는다(검증은 require_google_user). 묶음 키로만 쓰고 원문은 남기지 않는다
            limits = [('ip:' + host, RATE_LIMIT_PER_IP_WINDOW)]
            limits.append(('user:' + hashlib.sha256(token.encode()).hexdigest()[:32], RATE_LIMIT_PER_WINDOW) if token
                          else ('ip-anon:' + host, RATE_LIMIT_PER_WINDOW))
            blocked = None
            with rate_lock:
                if len(rates) > 5000:
                    for old in [k for k, value in rates.items() if now - value[0] > RATE_WINDOW_SECONDS]:
                        del rates[old]
                for key, limit in limits:
                    started, count = rates.get(key, (now, 0))
                    if now - started >= RATE_WINDOW_SECONDS:
                        started, count = now, 0
                    rates[key] = started, count + 1
                    if count >= limit and blocked is None:
                        blocked = started
            if blocked is not None:
                retry_after = max(1, math.ceil(RATE_WINDOW_SECONDS - (now - blocked)))
                response = api_error('RATE_LIMITED', '요청이 너무 많습니다.', 429, headers={'Retry-After': str(retry_after)})
        if response is None:
            try:
                response = await call_next(request)
            except Exception as error:
                # Handled here (not only in the outermost server-error handler) so the error keeps
                # X-Request-ID, no-store and CORS headers like every other response.
                log_unexpected(request, error, request_id)
                response = api_error('PROCESSING_UNAVAILABLE', '요청을 처리하지 못했습니다.', 503)
        response.headers['Cache-Control'] = 'private, no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        # Exact idempotent replay preserves the first envelope and its request ID.
        response.headers.setdefault('X-Request-ID', request_id)
        return response

    # Added last so it is the outermost middleware: 429/413 responses also carry CORS headers for the browser.
    app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origins, allow_credentials=False,
                       allow_methods=['GET', 'POST', 'PATCH', 'PUT'],
                       allow_headers=['Authorization', 'Content-Type', 'Idempotency-Key', 'Cache-Control'],
                       expose_headers=['X-Request-ID', 'Retry-After', 'Content-Disposition'])

    @app.exception_handler(ServiceError)
    async def service_error(request, error):
        if error.status >= 500:
            logger.error('request failed status=%d code=%s path_template=%s request_id=%s %s', error.status, error.code,
                         route_template(request), current_request_id(), error_origin(error))
        return service_error_response(error)

    @app.exception_handler(OwnershipLost)
    async def ownership_error(request, error):
        return api_error('REVISION_CONFLICT', '업무 상태 또는 작업 소유권이 변경됐습니다. 최신 내용을 확인해 주세요.', 409)

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request, error):
        if error.status_code == 404:
            return api_error('RESOURCE_NOT_FOUND', '요청한 리소스를 찾을 수 없습니다.', 404, headers=error.headers)
        if error.status_code == 405:
            return api_error('METHOD_NOT_ALLOWED', '허용하지 않은 요청 방식입니다.', 405, headers=error.headers)
        return api_error('HTTP_ERROR', '요청을 처리할 수 없습니다.', error.status_code, headers=error.headers)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request, error):
        fields = ['.'.join(str(part) for part in issue['loc']) for issue in error.errors()]
        return api_error('INVALID_REQUEST', '요청 형식이 올바르지 않습니다.', 422, fields=fields)

    @app.exception_handler(Exception)
    async def unexpected(request, error):
        log_unexpected(request, error, current_request_id())
        return api_error('PROCESSING_UNAVAILABLE', '요청을 처리하지 못했습니다.', 503)

    app.include_router(system_router)
    app.include_router(mail_router)
    app.include_router(mailbox_router)
    app.include_router(work_cases_router)
    app.include_router(work_revisions_router)
    app.include_router(workflow_logs_router)
    app.include_router(suppliers_router)
    app.include_router(mail_drafts_router)
    app.include_router(candidates_router)
    app.include_router(quotes_router)
    app.include_router(review_card_router)
    app.include_router(mail_management_router)
    app.include_router(call_recordings_router)
    return app


app = create_app()
