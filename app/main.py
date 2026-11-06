"""Application assembly. Feature endpoints live under app/features/*/api."""
import logging
import math
import threading
import time
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from starlette.exceptions import HTTPException as StarletteHTTPException
from app.api.responses import ErrorEnvelope, api_error, service_error_response
from app.core.config import get_settings
from app.core.request_context import new_request_id
from app.shared.infrastructure.db.client import make_server_client
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
from app.features.work_cases.api.router import router as work_cases_router
from app.features.work_cases.api.revisions import router as work_revisions_router
from app.features.workflow_logs.router import router as workflow_logs_router
from app.features.suppliers.router import router as suppliers_router
from app.features.work_cases.api.mail_drafts import router as mail_drafts_router
from app.features.work_cases.api.candidates import router as candidates_router
from app.features.mail_management.api.router import router as mail_management_router
from app.features.system.application.diagnostics import require_startup_configuration

logger = logging.getLogger('gyeonjwo')
RATE_WINDOW_SECONDS = 60
RATE_LIMIT_PER_WINDOW = 120
RATE_LIMIT_EXEMPT_PATHS = frozenset({'/api/v1/webhooks/gmail'})

def start_metadata_worker(settings, mailbox_service):
    """보낸 RFQ의 메일 헤더를 기록하는 워커를 만든다. 스키마가 없으면 경고만 남기고 None을 돌려준다."""
    from app.features.work_cases.infrastructure.rfq_repository import RfqRepository
    from app.features.work_cases.application.rfq_metadata_worker import RfqMetadataWorker
    repo = RfqRepository(make_server_client(settings))
    try:
        repo.require_schema()
    except Exception:
        logger.warning('rfq metadata worker unavailable code=SCHEMA_REQUIRED; API remains available')
        return None
    return RfqMetadataWorker(repo, mailbox_service, repo.metadata_owners)


def start_workflow_worker(settings, mailbox_service):
    """RFQ 준비·발송 워커를 만든다. 발송 여부는 사용자별 발송 설정이 정한다. 스키마가 없으면 경고만 남기고 None."""
    from app.features.work_cases.infrastructure.rfq_delivery_repository import RfqDeliveryRepository
    from app.features.work_cases.application.rfq_preparation import RfqPreparation
    from app.features.work_cases.application.rfq_delivery import RfqDelivery
    from app.features.work_cases.application.rfq_workflow_worker import RfqWorkflowWorker
    from app.shared.infrastructure.llm import JsonAgent
    repo = RfqDeliveryRepository(make_server_client(settings))
    try:
        repo.require_schema()
        repo.require_http_schema()
    except Exception:
        logger.warning('rfq workflow worker unavailable code=SCHEMA_REQUIRED; API remains available')
        return None
    return RfqWorkflowWorker(repo, RfqPreparation(repo, JsonAgent(settings), settings.llm_model),
                             RfqDelivery(repo, mailbox_service), repo.metadata_owners)


def create_app(settings=None, services=None, push_verifier=verify_push, mailbox_service=None):
    settings = settings or get_settings()
    require_startup_configuration(settings)

    @asynccontextmanager
    async def lifespan(app):
        connected_worker = None
        extraction_worker = None
        rfq_worker = None
        rfq_workflow = None
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
            if connected_worker is not None:
                rfq_worker = start_metadata_worker(settings, app.state.mailbox_service)
                app.state.rfq_metadata_worker = rfq_worker
                rfq_workflow = start_workflow_worker(settings, app.state.mailbox_service)
                app.state.rfq_workflow_worker = rfq_workflow
            # Graph readiness is optional for receiving mail and serving APIs.
            # No feature toggle: attempt the existing START contract each boot.
            from app.features.work_cases.infrastructure.graph_repository import runtime_repository
            from app.features.work_cases.application.graph_worker import GraphWorker
            from app.features.work_cases.application.start_readiness import configuration_readiness, agent_readiness
            try:
                configuration_readiness(settings).require()
                agent_readiness().require()
                repo = runtime_repository(make_server_client(settings), settings)
                if not repo.available():
                    raise RuntimeError('Graph START contract unavailable')
                if app.state.mailbox_service is None:
                    app.state.mailbox_service = make_mailbox_service(settings)
                extraction_worker = GraphWorker(settings, repo, app.state.mailbox_service)
                extraction_worker.start()
                app.state.extraction_worker = extraction_worker
                app.state.graph_startup_code = None
            except Exception:
                if extraction_worker is not None:
                    try:
                        extraction_worker.close()
                    except Exception:
                        logger.warning('graph cleanup failed code=GRAPH_CLEANUP_FAILED')
                extraction_worker = None
                app.state.extraction_worker = None
                app.state.graph_startup_code = 'GRAPH_RUNTIME_UNAVAILABLE'
                logger.warning('graph unavailable code=GRAPH_RUNTIME_UNAVAILABLE; mail/API remain available')
            # Independent mail/metadata lifecycles; sender remains separately gated.
            for prepared_worker in (connected_worker, rfq_worker, rfq_workflow):
                if prepared_worker is not None:
                    prepared_worker.start()
            yield
        finally:
            if rfq_workflow:
                rfq_workflow.close()
            if rfq_worker:
                rfq_worker.close()
            if connected_worker:
                connected_worker.close()
            if extraction_worker:
                extraction_worker.close()

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
    app.include_router(mail_management_router)
    return app


app = create_app()
