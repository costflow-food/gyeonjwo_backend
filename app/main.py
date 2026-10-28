"""Application assembly. Feature endpoints live under app/features/*/api."""
import logging
import threading
import time
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from app.api.responses import api_json
from app.core.config import get_settings
from app.shared.infrastructure.db.client import make_server_client
from app.shared.infrastructure.db.contracts import OwnershipLost, WorkNotFound
from app.core.errors import ServiceError
from app.features.mail.infrastructure.gmail import verify_push
from app.features.review.application.services import Services
from app.features.mail.application.worker import GmailWorker
from app.core.http_limits import BodyLimitMiddleware
from app.features.system.api.router import router as system_router
from app.features.mail.api.router import router as mail_router
from app.features.review.api.router import router as review_router

logger = logging.getLogger('gyeonjwo')

def create_app(settings=None, services=None, push_verifier=verify_push):
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app):
        worker = None
        if settings.worker_enabled:
            settings.require_server()
            settings.require_gmail()
            settings.require_llm()
            if app.state.services is None:
                app.state.services = Services(settings, make_server_client(settings))
            worker = GmailWorker(app.state.services)
            app.state.worker = worker
            worker.start()
        try:
            yield
        finally:
            if worker:
                worker.close()

    app = FastAPI(title='견줘 Python Backend', version='1.0.0', lifespan=lifespan)
    app.state.settings, app.state.services, app.state.worker = settings, services, None
    app.state.service_lock = threading.Lock()
    app.state.push_verifier = push_verifier
    app.add_middleware(BodyLimitMiddleware)
    app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origins, allow_credentials=False,
                       allow_methods=['GET', 'POST', 'PATCH'], allow_headers=['Authorization', 'Content-Type'])
    rate_lock, rates = threading.Lock(), {}

    @app.middleware('http')
    async def headers_and_limits(request, call_next):
        now = time.monotonic()
        key = request.client.host if request.client else 'unknown'
        with rate_lock:
            if len(rates) > 5000:
                for old in [k for k, value in rates.items() if now - value[0] > 60]:
                    del rates[old]
            started, count = rates.get(key, (now, 0))
            if now - started >= 60:
                started, count = now, 0
            rates[key] = started, count + 1
        if count >= 120:
            response = api_json({'error': {'code': 'RATE_LIMITED', 'message': '요청이 너무 많습니다.', 'details': {}}}, 429)
        elif request.headers.get('content-length', '').isdigit() and int(request.headers['content-length']) > 65536:
            response = api_json({'error': {'code': 'REQUEST_TOO_LARGE', 'message': '요청 본문이 너무 큽니다.', 'details': {}}}, 413)
        else:
            response = await call_next(request)
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        return response

    @app.exception_handler(ServiceError)
    async def service_error(request, error):
        return api_json({'error': {'code': error.code, 'message': error.message, 'details': error.details}}, error.status)

    @app.exception_handler(OwnershipLost)
    async def ownership_error(request, error):
        return api_json({'error': {'code': 'INVALID_WORK_STATE', 'message': '업무 상태 또는 작업 소유권이 변경됐습니다.', 'details': {}}}, 409)

    @app.exception_handler(WorkNotFound)
    async def missing_work(request, error):
        return api_json({'error': {'code': 'WORK_NOT_FOUND', 'message': '업무를 찾을 수 없습니다.', 'details': {}}}, 404)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request, error):
        return api_json({'error': {'code': 'INVALID_REQUEST', 'message': '요청 형식이 올바르지 않습니다.',
                                    'details': {'fields': [list(issue['loc']) for issue in error.errors()]}}}, 422)

    @app.exception_handler(Exception)
    async def unexpected(request, error):
        logger.error('request failed path_template=%s', getattr(request.scope.get('route'), 'path', 'unknown'))
        return api_json({'error': {'code': 'SERVICE_UNAVAILABLE', 'message': '요청을 처리하지 못했습니다.', 'details': {}}}, 503)

    app.include_router(system_router)
    app.include_router(mail_router)
    app.include_router(review_router)
    return app


app = create_app()
