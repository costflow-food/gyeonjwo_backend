from typing import Annotated, Literal

from fastapi import Depends, FastAPI, File, Query, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException

from .auth import LoginLimiter, Principal, authorize, bearer, require_reviewer, require_user
from .config import Settings
from .errors import AppError
from .gateway import SupabaseGateway
from .models import (
    AnalyzeOut,
    AnalyzeRequest,
    DecisionRequest,
    FieldsRequest,
    FileId,
    LoginRequest,
    RefreshRequest,
    RejectRequest,
    SignedLinkRequest,
    UploadOut,
    WorkCreate,
    WorkDetail,
    WorkId,
    WorkOut,
    WorkPage,
    WorkStatus,
)
from .service import CostFlowService


class PayloadTooLarge(HTTPException):
    def __init__(self):
        super().__init__(status_code=413, detail="요청 본문이 허용 크기를 초과했습니다.")


class BodyLimitMiddleware:
    def __init__(self, app, limit):
        self.app, self.limit = app, limit

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        seen, started = 0, False

        async def limited_receive():
            nonlocal seen
            message = await receive()
            seen += len(message.get("body", b""))
            if seen > self.limit:
                raise PayloadTooLarge()
            return message

        async def no_store_send(message):
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
                message.setdefault("headers", []).extend(
                    [(b"cache-control", b"no-store"), (b"x-content-type-options", b"nosniff")]
                )
            await send(message)

        try:
            await self.app(scope, limited_receive, no_store_send)
        except PayloadTooLarge:
            if started:
                raise
            await JSONResponse(
                status_code=413,
                content={
                    "error": {
                        "code": "body_too_large",
                        "message": "요청 본문이 허용 크기를 초과했습니다.",
                    }
                },
            )(scope, receive, no_store_send)


def create_app(settings: Settings | None = None, gateway=None) -> FastAPI:
    settings = settings or Settings.from_env()
    app = FastAPI(
        title="CostFlow API",
        version="0.1.0",
        debug=False,
        description="허용된 팀 사용자 전용 API. 기준자료는 읽기 전용이며 최종 결정은 검토자가 수행합니다.",
    )
    app.state.settings = settings
    app.state.gateway = gateway or SupabaseGateway(settings)
    app.state.service = CostFlowService(app.state.gateway)
    app.state.login_limiter = LoginLimiter()
    # A strict cap also covers chunked requests, before multipart spooling finishes.
    app.add_middleware(BodyLimitMiddleware, limit=settings.max_upload_bytes + 1024 * 1024)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.cors_origins),
        allow_methods=["GET", "POST", "PUT"],
        allow_headers=["Authorization", "Content-Type"],
        allow_credentials=False,
    )

    @app.exception_handler(AppError)
    async def app_error(request, exc):
        headers = {"WWW-Authenticate": "Bearer"} if exc.status == 401 else None
        return JSONResponse(
            status_code=exc.status,
            content={"error": {"code": exc.code, "message": exc.message}},
            headers=headers,
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        # Pydantic's raw `input` may contain passwords, tokens or document text.
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "code": "validation_error",
                    "message": "입력값을 확인해 주세요.",
                    "fields": [
                        {"loc": list(e["loc"]), "type": e["type"], "message": e["msg"]}
                        for e in exc.errors()
                    ],
                }
            },
        )

    @app.get("/health", tags=["system"])
    def health():
        return {"status": "ok", "version": "0.1.0"}

    @app.exception_handler(HTTPException)
    async def http_error(request, exc):
        return JSONResponse(
            status_code=exc.status_code,
            headers=exc.headers,
            content={"error": {"code": "http_error", "message": str(exc.detail)}},
        )

    @app.get("/ready", tags=["system"])
    def ready():
        if not settings.ready or not (settings.users | settings.reviewers):
            raise AppError(
                503, "not_configured", "서버 연결 및 사용자 허용 목록 설정이 필요합니다."
            )
        app.state.gateway.rows("work_items", limit=1)
        return {"status": "ready"}

    prefix = "/api/v1"

    def token_response(result):
        access = result.get("access_token")
        if not access:
            raise AppError(502, "invalid_session", "로그인 세션 응답이 올바르지 않습니다.")
        user = authorize(app.state.gateway.user(access), settings)
        return {
            "access_token": access,
            "refresh_token": result.get("refresh_token"),
            "token_type": "bearer",
            "expires_in": result.get("expires_in"),
            "user": {"id": user.user_id, "reviewer": user.reviewer},
        }

    @app.post(prefix + "/auth/login", tags=["auth"])
    def login(body: LoginRequest, request: Request):
        app.state.login_limiter.check(request.client.host if request.client else "unknown")
        return token_response(
            app.state.gateway.token(
                "password", {"email": body.email, "password": body.password.get_secret_value()}
            )
        )

    @app.post(prefix + "/auth/refresh", tags=["auth"])
    def refresh(body: RefreshRequest, request: Request):
        app.state.login_limiter.check(request.client.host if request.client else "unknown")
        return token_response(
            app.state.gateway.token(
                "refresh_token", {"refresh_token": body.refresh_token.get_secret_value()}
            )
        )

    @app.post(prefix + "/auth/logout", tags=["auth"])
    def logout(user: Principal = Depends(require_user), credentials=Depends(bearer)):
        app.state.gateway.logout(credentials.credentials)
        return {"logged_out": True}

    @app.get(prefix + "/auth/me", tags=["auth"])
    def me(user: Principal = Depends(require_user)):
        return {"id": user.user_id, "reviewer": user.reviewer}

    @app.post(prefix + "/work-items", status_code=201, response_model=WorkOut, tags=["work"])
    def create_work(body: WorkCreate, user: Principal = Depends(require_user)):
        return app.state.service.create(body.classification_reason)

    @app.get(prefix + "/work-items", response_model=WorkPage, tags=["work"])
    def list_work(
        status: WorkStatus | None = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 25,
        offset: Annotated[int, Query(ge=0, le=100000)] = 0,
        user: Principal = Depends(require_user),
    ):
        filters = {"status": "eq." + status.value} if status else {}
        rows = app.state.gateway.rows(
            "work_items", filters=filters, limit=limit + 1, offset=offset, order="work_id.desc"
        )
        return {
            "items": rows[:limit],
            "offset": offset,
            "limit": limit,
            "has_more": len(rows) > limit,
        }

    @app.get(prefix + "/work-items/{work_id}", response_model=WorkDetail, tags=["work"])
    def work_detail(work_id: WorkId, user: Principal = Depends(require_user)):
        return app.state.service.detail(work_id)

    @app.post(prefix + "/work-items/{work_id}/reject", response_model=WorkOut, tags=["work"])
    def reject(work_id: WorkId, body: RejectRequest, user: Principal = Depends(require_reviewer)):
        return app.state.service.reject(work_id, body.reason, user.user_id)

    @app.put(prefix + "/work-items/{work_id}/request-fields", tags=["analysis"])
    def fields(work_id: WorkId, body: FieldsRequest, user: Principal = Depends(require_user)):
        return app.state.service.save_fields(work_id, body)

    @app.post(
        prefix + "/work-items/{work_id}/analyze", response_model=AnalyzeOut, tags=["analysis"]
    )
    def analyze(work_id: WorkId, body: AnalyzeRequest, user: Principal = Depends(require_user)):
        return app.state.service.analyze(work_id, body.file_ids)

    @app.get(prefix + "/work-items/{work_id}/review", tags=["review"])
    def review(
        work_id: WorkId,
        offset: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=25)] = 10,
        user: Principal = Depends(require_user),
    ):
        return app.state.service.review(work_id, offset, limit)

    @app.post(
        prefix + "/work-items/{work_id}/decisions", response_model=WorkDetail, tags=["review"]
    )
    def decision(
        work_id: WorkId, body: DecisionRequest, user: Principal = Depends(require_reviewer)
    ):
        return app.state.service.decide(work_id, body, user)

    @app.post(
        prefix + "/work-items/{work_id}/files",
        status_code=201,
        response_model=UploadOut,
        tags=["files"],
    )
    def upload(
        work_id: WorkId, file: UploadFile = File(...), user: Principal = Depends(require_user)
    ):
        return app.state.service.upload(work_id, file)

    @app.get(prefix + "/work-items/{work_id}/files", tags=["files"])
    def files(
        work_id: WorkId,
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
        offset: Annotated[int, Query(ge=0)] = 0,
        user: Principal = Depends(require_user),
    ):
        return app.state.service.files(work_id, limit, offset)

    @app.post(prefix + "/work-items/{work_id}/files/{file_id}/signed-url", tags=["files"])
    def signed_url(
        work_id: WorkId,
        file_id: FileId,
        body: SignedLinkRequest,
        user: Principal = Depends(require_user),
    ):
        return app.state.service.signed_link(work_id, file_id, body.expires_in)

    @app.get(prefix + "/reference-data/{table}", tags=["reference-data"])
    def reference_data(
        table: Literal[
            "suppliers", "items", "contracts", "purchase_plans", "price_references", "cost_terms"
        ],
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
        offset: Annotated[int, Query(ge=0, le=100000)] = 0,
        user: Principal = Depends(require_user),
    ):
        orders = {
            "suppliers": "supplier_id",
            "items": "item_id",
            "contracts": "contract_id",
            "purchase_plans": "plan_id",
            "price_references": "reference_id",
            "cost_terms": "owner_type,owner_id,cost_type",
        }
        rows = app.state.gateway.rows(table, limit=limit + 1, offset=offset, order=orders[table])
        return {
            "items": rows[:limit],
            "offset": offset,
            "limit": limit,
            "has_more": len(rows) > limit,
        }

    return app


app = create_app()
