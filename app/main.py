"""Application assembly. Feature endpoints live under app/features/*/api."""
import asyncio
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
from app.core.request_context import new_request_id
from app.shared.infrastructure.db.contracts import OwnershipLost, WorkNotFound
from app.core.errors import ServiceError
from app.features.mail.infrastructure.gmail import verify_push
from app.features.review.application.services import Services
from app.core.http_limits import BodyLimitMiddleware
from app.features.system.api.router import router as system_router
from app.features.mail.api.router import router as mail_router
from app.features.review.api.router import router as review_router
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
    create_missing_rfq_workers, retry_unavailable_workers, start_graph_worker)

logger = logging.getLogger('gyeonjwo')
RATE_WINDOW_SECONDS = 60
RATE_LIMIT_PER_WINDOW = 120
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


def create_app(settings=None, services=None, push_verifier=verify_push, mailbox_service=None):
    settings = settings or get_settings()
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
            await prime_work_case_reads(app.state)
            # Independent mail/metadata lifecycles; sender remains separately gated.
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
            for name in ('rfq_workflow_worker', 'rfq_metadata_worker', 'connected_mailbox_worker', 'extraction_worker'):
                worker = getattr(app.state, name, None)
                if worker:
                    worker.close()

    # Explicitly override FastAPI's default HTTPValidationError schema: runtime uses ErrorEnvelope.
    common_errors = {status: {'model': ErrorEnvelope} for status in
                     (400, 401, 403, 404, 405, 409, 413, 422, 429, 502, 503, 504)}
    app = FastAPI(title='견줘 Python Backend', version='1.0.0', lifespan=lifespan,
                  responses=common_errors)
    app.state.settings, app.state.services = settings, services
    app.state.mailbox_service = mailbox_service
    app.state.connected_mailbox_worker = None
    app.state.extraction_worker = None
    app.state.graph_startup_code = 'GRAPH_RUNTIME_UNAVAILABLE'
    app.state.rfq_metadata_worker = None
    app.state.rfq_workflow_worker = None
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
            key = request.client.host if request.client else 'unknown'
            with rate_lock:
                if len(rates) > 5000:
                    for old in [k for k, value in rates.items() if now - value[0] > RATE_WINDOW_SECONDS]:
                        del rates[old]
                started, count = rates.get(key, (now, 0))
                if now - started >= RATE_WINDOW_SECONDS:
                    started, count = now, 0
                rates[key] = started, count + 1
            if count >= RATE_LIMIT_PER_WINDOW:
                retry_after = max(1, math.ceil(RATE_WINDOW_SECONDS - (now - started)))
                response = api_error('RATE_LIMITED', '요청이 너무 많습니다.', 429, headers={'Retry-After': str(retry_after)})
        if response is None:
            try:
                response = await call_next(request)
            except Exception:
                # Handled here (not only in the outermost server-error handler) so the error keeps
                # X-Request-ID, no-store and CORS headers like every other response.
                logger.error('request failed path_template=%s', getattr(request.scope.get('route'), 'path', 'unknown'))
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
        return service_error_response(error)

    @app.exception_handler(OwnershipLost)
    async def ownership_error(request, error):
        return api_error('REVISION_CONFLICT', '업무 상태 또는 작업 소유권이 변경됐습니다. 최신 내용을 확인해 주세요.', 409)

    @app.exception_handler(WorkNotFound)
    async def missing_work(request, error):
        return api_error('RESOURCE_NOT_FOUND', '업무를 찾을 수 없습니다.', 404)

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
        logger.error('request failed path_template=%s', getattr(request.scope.get('route'), 'path', 'unknown'))
        return api_error('PROCESSING_UNAVAILABLE', '요청을 처리하지 못했습니다.', 503)

    app.include_router(system_router)
    app.include_router(mail_router)
    app.include_router(review_router)
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
    return app


app = create_app()
