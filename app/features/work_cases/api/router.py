from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, Header
from fastapi.responses import JSONResponse
from uuid import UUID
import time
from pydantic import Field
from app.shared.domain.schemas import StrictModel
from app.core.errors import ServiceError
from app.features.work_cases.application.actions import WorkCaseActions
from app.features.work_cases.infrastructure.runtime_repository import CaseRuntimeRepository
from app.features.work_cases.domain.actions import DeletedCase, StartedCase

from app.api.responses import Envelope, api_json
from app.features.mailboxes.api.router import require_google_user
from app.features.work_cases.application.cursor import CursorCodec, utc_time, iso_z
from app.features.work_cases.application.list_service import WorkCaseListService
from app.features.work_cases.domain.listing import QueueFilter, WorkCaseList
from app.features.work_cases.infrastructure.list_repository import WorkCaseListRepository
from app.shared.infrastructure.db.client import make_server_client

from app.features.work_cases.application.agent_boundary import (
    extraction_adapter_available, load_extraction_adapter)

router = APIRouter(prefix='/api/v1/work-cases', tags=['Work cases'])


def extraction_available(request):
    settings = request.app.state.settings
    if (not settings.agent_extraction_enabled or not settings.agent_stop_after_extraction
            or not extraction_adapter_available()):
        return False
    with request.app.state.service_lock:
        cached = getattr(request.app.state, 'extraction_availability', None)
        now = time.monotonic()
        if cached is not None and now < cached[0]:
            return cached[1]
        try:
            available = CaseRuntimeRepository(make_server_client(settings)).available()
        except ServiceError:
            available = False
        request.app.state.extraction_availability = (now + 5, available)
        return available


def get_list_service(request: Request):
    with request.app.state.service_lock:
        service = getattr(request.app.state, 'work_case_list_service', None)
        if service is None:
            settings = request.app.state.settings
            client = make_server_client(settings)
            service = WorkCaseListService(WorkCaseListRepository(client,
                                         extraction_fields=True),
                                         CursorCodec(settings.google_token_encryption_key.get_secret_value()))
            request.app.state.work_case_list_service = service
    service.extraction_enabled = extraction_available(request)
    return service


EXAMPLE = {
    'data': {
        'items': [{
            'case_id': '4c1b2687-c86b-4aca-986f-3a5aa1aa1001',
            'initial_mail_id': '4c1b2687-c86b-4aca-986f-3a5aa1aa2001',
            'title': 'sales@example.com', 'sender': 'sales@example.com', 'sender_name': None, 'requester_name': None,
            'received_at': '2026-10-29T06:00:00.000000Z', 'status': 'Waiting',
            'processing_step': 'PENDING_START', 'input_revision': 1, 'archived_at': None,
            'item_name': None, 'attachment_count': 1,
            'step_detail': {'code': 'PENDING_START', 'message': '원문 확인 후 검토 시작'},
            'reply_summary': {'requested_supplier_count': 0, 'responded_supplier_count': 0, 'reply_rate': None},
            'allowed_actions': ['VIEW_ORIGINAL', 'ACCEPT_INCREASE'], 'disabled_actions': [],
        }],
        'next_cursor': None, 'counts': {'Waiting': 1, 'Running': 0, 'Complete': 0, 'total': 1},
        'intake_pending': 0,
    },
    'meta': {'request_id': 'req-example-01'},
}


@router.get('', response_model=Envelope[WorkCaseList], summary='업무 큐 목록·검색·상태별 건수',
            responses={200: {'content': {'application/json': {'example': EXAMPLE}}}})
def list_work_cases(
    principal=Depends(require_google_user), service=Depends(get_list_service),
    status: QueueFilter | None = None,
    archived: bool = False,
    q: Annotated[str, Query(max_length=200)] = '',
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    cursor: Annotated[str | None, Query(max_length=2048)] = None,
):
    """Read only: never starts extraction or changes work state.

    Reset cursor when status, archive filter or search changes. Counts ignore only
    status, not search/archive. Pending/Confirm rows remain unchanged when status
    is omitted; total includes them, while named counters use API-01's three tabs.
    sender_name is null until its persistence contract is implemented. Other actions
    remain conservatively hidden until their individual endpoint guards are wired.
    reply_summary counts distinct suppliers from SENT RFQs and verified quote
    replies in the current request round. Sidebar totals belong to the frontend.
    """
    result = service.list(principal.user_id, status=status, archived=archived, q=q, limit=limit, cursor=cursor)
    return api_json(result.model_dump(mode='json'), headers={'Cache-Control': 'private, no-store'})


class CaseActionRequest(StrictModel):
    expected_revision: int = Field(strict=True, ge=1)


def get_runtime_repository(request: Request):
    with request.app.state.service_lock:
        repo = getattr(request.app.state, 'case_runtime_repository', None)
        if repo is None:
            repo = CaseRuntimeRepository(make_server_client(request.app.state.settings))
            request.app.state.case_runtime_repository = repo
        return repo


def stored_case_row(rows, required):
    """Distinguish missing rows from corrupt storage replies without leaking them."""
    if (not isinstance(rows, list) or len(rows) > 1
            or (rows and (not isinstance(rows[0], dict)
                          or any(field not in rows[0] for field in required)))):
        raise ServiceError('PROCESSING_UNAVAILABLE', '업무 조회 결과를 확인할 수 없습니다.', 503)
    if not rows:
        raise ServiceError('RESOURCE_NOT_FOUND', '업무를 찾을 수 없습니다.', 404)
    return dict(rows[0])


def action_response(request, principal, repo, case_id, payload, key, action):
    enabled = (request.app.state.settings.agent_extraction_enabled
               and request.app.state.settings.agent_stop_after_extraction)
    if action == 'START' and enabled:
        load_extraction_adapter()  # Fail before accepting a job that cannot execute.
    if action == 'START' and enabled and not repo.available():
        raise ServiceError('EXTRACTION_SCHEMA_REQUIRED', '검토 실행 DB 계약을 먼저 적용해야 합니다.', 503, retryable=False)
    status, response = WorkCaseActions(repo, extraction_enabled=enabled).submit(
        principal.user_id, case_id, payload.expected_revision, action, key)
    worker = getattr(request.app.state, 'extraction_worker', None)
    if action == 'START' and worker:
        worker.wake.set()
    return JSONResponse(response, status_code=status, headers={
        'X-Request-ID': response['meta']['request_id'], 'Cache-Control': 'private, no-store'})


@router.post('/{case_id}/accept-increase', response_model=Envelope[DeletedCase], summary='수용: 업무 삭제·재수신 방지 영수증만 보존')
def accept_increase(case_id: UUID, payload: CaseActionRequest, request: Request,
                    principal=Depends(require_google_user), repo=Depends(get_runtime_repository),
                    idempotency_key: Annotated[str | None, Header(alias='Idempotency-Key')] = None):
    """Irreversible. Frontend confirms before sending. Never calls an agent."""
    return action_response(request, principal, repo, case_id, payload, idempotency_key, 'ACCEPT_INCREASE')


@router.post('/{case_id}/start', status_code=202, response_model=Envelope[StartedCase], summary='검토 접수: ① 추출을 백그라운드에서 실행')
def start_case(case_id: UUID, payload: CaseActionRequest, request: Request,
               principal=Depends(require_google_user), repo=Depends(get_runtime_repository),
               idempotency_key: Annotated[str | None, Header(alias='Idempotency-Key')] = None):
    return action_response(request, principal, repo, case_id, payload, idempotency_key, 'START')


@router.get('/{case_id}/progress', summary='저장된 검토 단계 조회')
def case_progress(case_id: UUID, principal=Depends(require_google_user), repo=Depends(get_runtime_repository)):
    try:
        rows = (repo.client.table('work_cases')
                .select('id,status,processing_step,input_revision,error_jsonb,updated_at')
                .eq('owner_user_id', principal.user_id).eq('id', str(case_id)).limit(1).execute().data)
    except Exception:
        raise ServiceError('PROCESSING_UNAVAILABLE', '처리 단계를 조회할 수 없습니다.', 503) from None
    row = stored_case_row(rows, ('id', 'status', 'processing_step', 'input_revision'))
    safe_codes = {'EXTRACTION_FAILED', 'EXTRACTION_TIME_LIMIT', 'WORKER_INTERRUPTED', 'INPUT_REVISION_CHANGED',
                  'RFQ_PREPARATION_FAILED', 'NO_REGISTERED_SUPPLIER_EVIDENCE', 'NO_SUITABLE_CANDIDATES',
                  'SEND_POLICY_REQUIRED', 'SEND_RESULT_UNKNOWN', 'SETTINGS_CHANGED', 'RFQ_CONDITIONS_REQUIRED'}
    error = row.get('error_jsonb')
    if error is not None and not isinstance(error, dict):
        raise ServiceError('PROCESSING_UNAVAILABLE', '저장된 오류 상태를 확인할 수 없습니다.', 503)
    code = (error or {}).get('code')
    try:
        updated_at = iso_z(utc_time(row['updated_at'])) if row.get('updated_at') else None
    except (ValueError, AttributeError, TypeError):
        raise ServiceError('PROCESSING_UNAVAILABLE', '저장된 시각 형식을 확인할 수 없습니다.', 503) from None
    return api_json({'case_id': row['id'], 'status': row['status'], 'processing_step': row['processing_step'],
                     'input_revision': row['input_revision'], 'updated_at': updated_at,
                     'error': {'code': code if isinstance(code, str) and code in safe_codes else 'EXTRACTION_FAILED'} if code else None})


@router.get('/{case_id}', summary='업무의 저장 상태 조회: 실행 없음')
def get_case(case_id: UUID, principal=Depends(require_google_user), repo=Depends(get_runtime_repository)):
    try:
        rows = (repo.client.table('work_cases')
                .select('id,initial_mail_id,status,processing_step,input_revision,item_name,specification,quantity::text,unit')
                .eq('owner_user_id', principal.user_id).eq('id', str(case_id)).limit(1).execute().data)
    except Exception:
        raise ServiceError('PROCESSING_UNAVAILABLE', '업무를 조회할 수 없습니다.', 503) from None
    row = stored_case_row(rows, ('id', 'initial_mail_id', 'status', 'processing_step', 'input_revision',
                                 'item_name', 'specification', 'quantity', 'unit'))
    return api_json({'case_id': row.pop('id'), **row})
