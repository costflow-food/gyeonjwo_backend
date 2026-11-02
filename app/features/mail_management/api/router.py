from typing import Annotated
from uuid import UUID
from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.responses import JSONResponse
from app.api.responses import Envelope, api_json
from app.features.mailboxes.api.router import require_google_user
from app.features.mail_management.application.service import MailManagementService
from app.features.mail_management.domain.models import (
    IntakePage, ResolvedMail, ResolveRequest, SendSettings, SettingsUpdate)
from app.features.mail_management.infrastructure.repository import MailManagementRepository
from app.features.work_cases.application.cursor import CursorCodec
from app.shared.infrastructure.db.client import make_server_client

router = APIRouter(prefix='/api/v1', tags=['Mail intake and send settings'])
PRIVATE = {'Cache-Control': 'private, no-store'}


def get_management_service(request: Request):
    with request.app.state.service_lock:
        service = getattr(request.app.state, 'mail_management_service', None)
        if service is None:
            settings = request.app.state.settings
            service = MailManagementService(MailManagementRepository(make_server_client(settings)),
                CursorCodec(settings.google_token_encryption_key.get_secret_value()))
            request.app.state.mail_management_service = service
        return service


@router.get('/mail-intake', response_model=Envelope[IntakePage], summary='API-23: 접수 판단 대기 메일 조회')
def intake(principal=Depends(require_google_user), service=Depends(get_management_service),
           cursor: Annotated[str | None, Query(max_length=2048)] = None,
           limit: Annotated[int, Query(ge=1, le=100)] = 25):
    """Owner-scoped NEEDS_REVIEW + UNCERTAIN/AMBIGUOUS only; no analysis.

    total_pending uses the same definition as API-01 intake_pending. Cursor is
    signed and resource/owner scoped. revision is a safe integer snapshot token,
    not work_cases.input_revision. Attachments here are filenames as in SCR-009.
    """
    return api_json(service.intake(principal.user_id, cursor=cursor, limit=limit).model_dump(mode='json'), headers=PRIVATE)


def saved_response(response):
    return JSONResponse(response, headers={**PRIVATE, 'X-Request-ID': response['meta']['request_id']})


@router.post('/mail-messages/{mail_id}/resolve', response_model=Envelope[ResolvedMail],
             summary='API-24: 비업무·새 업무·기존 업무 연결 판단')
def resolve(mail_id: UUID, payload: ResolveRequest, request: Request,
            principal=Depends(require_google_user), service=Depends(get_management_service),
            idempotency_key: Annotated[str | None, Header(alias='Idempotency-Key')] = None):
    """One durable mutation transaction; replay does not reclassify/requeue.

    New cases are Waiting. Linking alone never starts a Waiting/Confirm/Pending
    case. Only already active cases may enqueue downstream re-extraction.
    """
    response = service.resolve(principal.user_id, mail_id, payload, idempotency_key)
    worker = getattr(request.app.state, 'extraction_worker', None)
    if worker:
        worker.wake.set()
    return saved_response(response)


@router.get('/mail-send-settings', response_model=Envelope[SendSettings], summary='API-21: 메일함·업무 유효 발송 설정')
def settings(principal=Depends(require_google_user), service=Depends(get_management_service),
             mailbox_ref: Annotated[str | None, Query(min_length=1, max_length=300)] = None,
             case_id: UUID | None = None):
    """If mailbox_ref is omitted infer the case's or sole owned mailbox.

    No synthetic mailbox alias is accepted. The client must reuse mailbox_ref
    from this response when saving. No stored policy means configured=false.
    settings_version is an integer conflict token for BOTH source settings.
    Draft API-17/19 opaque settings_version remains a separate contract.
    """
    result = service.present_settings(principal.user_id, mailbox_ref, case_id)
    return api_json(result.model_dump(mode='json'), headers=PRIVATE)


@router.put('/mail-send-settings', response_model=Envelope[SendSettings], summary='API-22: 발송 설정 새 버전 저장')
def save_settings(payload: SettingsUpdate, request: Request,
                  principal=Depends(require_google_user), service=Depends(get_management_service),
                  idempotency_key: Annotated[str | None, Header(alias='Idempotency-Key')] = None):
    settings = request.app.state.settings
    worker = getattr(request.app.state, 'rfq_workflow_worker', None)
    dispatch_ready = (settings.rfq_workflow_enabled and settings.rfq_mail_sending_enabled
        and worker is not None and worker.running
        and str(principal.user_id) in {str(value) for value in settings.rfq_workflow_owner_ids})
    response = service.save_settings(principal.user_id, payload, idempotency_key, dispatch_ready=bool(dispatch_ready))
    if worker:
        worker.wake.set()
    return saved_response(response)
