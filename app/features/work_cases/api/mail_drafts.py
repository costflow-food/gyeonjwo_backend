from typing import Annotated
from uuid import UUID
from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.responses import JSONResponse
from app.api.responses import Envelope, api_json
from app.features.mailboxes.api.router import require_google_user
from app.features.work_cases.application.cursor import CursorCodec
from app.features.work_cases.application.mail_drafts import MailDraftService
from app.features.work_cases.domain.mail_drafts import ApproveDraftRequest, DraftAccepted
from app.features.work_cases.infrastructure.rfq_delivery_repository import RfqDeliveryRepository
from app.shared.domain.schemas import EmptyRequest
from app.shared.infrastructure.db.client import make_server_client

router = APIRouter(prefix='/api/v1', tags=['Mail drafts'])
PRIVATE = {'Cache-Control': 'private, no-store'}


def get_draft_service(request: Request):
    with request.app.state.service_lock:
        service = getattr(request.app.state, 'mail_draft_service', None)
        if service is None:
            settings = request.app.state.settings
            service = MailDraftService(RfqDeliveryRepository(make_server_client(settings)),
                CursorCodec(settings.google_token_encryption_key.get_secret_value()))
            request.app.state.mail_draft_service = service
        return service


@router.get('/work-cases/{case_id}/mail-drafts', summary='API-16: 목적별 초안·발송 상태 조회')
def list_drafts(case_id: UUID, principal=Depends(require_google_user), service=Depends(get_draft_service),
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    cursor: Annotated[str | None, Query(max_length=2048)] = None):
    return api_json(service.list(principal.user_id, case_id, limit=limit, cursor=cursor), headers=PRIVATE)


@router.get('/mail-drafts/{draft_id}', summary='API-17: 초안 스냅샷·유효 설정·발송 결과 조회')
def get_draft(draft_id: UUID, principal=Depends(require_google_user), service=Depends(get_draft_service)):
    return api_json(service.get(principal.user_id, draft_id), headers=PRIVATE)


def submit(request, principal, service, draft_id, action, payload, key):
    settings = request.app.state.settings
    worker = getattr(request.app.state, 'rfq_workflow_worker', None)
    accepting = (settings.rfq_workflow_enabled and worker is not None and worker.running
                 and str(principal.user_id) in {str(x) for x in settings.rfq_workflow_owner_ids}
                 and (action != 'APPROVE_SEND' or settings.rfq_mail_sending_enabled))
    response = service.submit(principal.user_id, draft_id, action, payload, key, bool(accepting))
    if worker is not None:
        worker.wake.set()
    return JSONResponse(response, status_code=202,
        headers={**PRIVATE, 'X-Request-ID': response['meta']['request_id']})


@router.post('/mail-drafts/{draft_id}/approve-and-send', status_code=202,
    response_model=Envelope[DraftAccepted], summary='API-19: 스냅샷 승인·비동기 발송 접수')
def approve(draft_id: UUID, payload: ApproveDraftRequest, request: Request,
    principal=Depends(require_google_user), service=Depends(get_draft_service),
    idempotency_key: Annotated[str | None, Header(alias='Idempotency-Key')] = None):
    """Use snapshot_hash and opaque settings_version from GET. 202 is not SENT."""
    return submit(request, principal, service, draft_id, 'APPROVE_SEND', payload, idempotency_key)


@router.post('/mail-drafts/{draft_id}/reconcile', status_code=202,
    response_model=Envelope[DraftAccepted], summary='API-20: UNKNOWN 발신함 대조 접수·재발송 없음')
def reconcile(draft_id: UUID, request: Request, payload: EmptyRequest | None = None,
    principal=Depends(require_google_user), service=Depends(get_draft_service),
    idempotency_key: Annotated[str | None, Header(alias='Idempotency-Key')] = None):
    return submit(request, principal, service, draft_id, 'RECONCILE', payload, idempotency_key)
