import time
from typing import Annotated, Literal
from concurrent.futures import ThreadPoolExecutor, TimeoutError

from fastapi import APIRouter, Depends, Query, Request, Header
from fastapi.responses import JSONResponse
from uuid import UUID
from datetime import date
from decimal import Decimal
from pydantic import Field, model_validator
from app.shared.domain.schemas import StrictModel
from app.core.errors import ServiceError
from app.features.work_cases.application.actions import WorkCaseActions, validate_action_key
from app.features.work_cases.infrastructure.graph_repository import runtime_repository
from app.features.work_cases.domain.actions import DeletedCase, StartedCase
from app.features.work_cases.api.decisions import DecisionRequest, RevisionRequest, case_changes_ready, submit_case_change

from app.api.responses import Envelope, api_json
from app.features.mailboxes.api.router import require_google_user
from app.features.work_cases.application.cursor import CursorCodec, utc_time, iso_z
from app.features.work_cases.application.list_service import STEP_MESSAGES, WorkCaseListService
from app.features.work_cases.infrastructure.hold_repository import HoldReasonRepository
from app.features.work_cases.infrastructure.resume_repository import ResumeEligibilityRepository
from app.features.work_cases.domain.listing import QueueFilter, WorkCaseList
from app.features.work_cases.infrastructure.list_repository import WorkCaseListRepository
from app.shared.infrastructure.db.client import make_server_client

from app.features.work_cases.application.start_readiness import (
    StartReadiness, configuration_readiness, agent_readiness, database_readiness, worker_readiness)
from app.features.work_cases.application.case_actions import case_actions

router = APIRouter(prefix='/api/v1/work-cases', tags=['Work cases'])
_LIST_READINESS = ThreadPoolExecutor(max_workers=2, thread_name_prefix='case-list-readiness')
# 직전 준비 상태 확인 결과를 이만큼 오래된 것까지 쓴다(초). 그보다 오래되면 '확인 중'으로 버튼을 끈다
READINESS_REUSE_SECONDS = 60


def _remember_readiness(state, done) -> None:
    """끝난 준비 상태 확인의 결과를 시각과 함께 남긴다. 실패한 확인은 남기지 않는다."""
    if done.cancelled() or done.exception() is not None:
        return
    state.work_case_list_readiness_done = (time.monotonic(), done.result())


def resume_readiness(request, repo=None, *, fresh=False):
    readiness = extraction_readiness(request, repo, fresh=fresh)
    if not readiness.available:
        return readiness
    return repository_resume_readiness(repo or get_runtime_repository(request))


def repository_resume_readiness(repository):
    try:
        return (StartReadiness() if repository.resume_available() else
                StartReadiness('RESUME_INTEGRATION_REQUIRED', '그래프 재개 저장 기능이 아직 준비되지 않았습니다.'))
    except Exception:
        return StartReadiness('PROCESSING_UNAVAILABLE', '재개 저장소의 연결 상태를 확인할 수 없습니다.')


def extraction_readiness(request, repo=None, *, fresh=False):
    settings = request.app.state.settings
    for probe in (lambda: configuration_readiness(settings), lambda: agent_readiness()):
        result = probe()
        if not result.available:
            return result
    try:
        result = database_readiness(repo or get_runtime_repository(request))
    except Exception:
        result = StartReadiness('PROCESSING_UNAVAILABLE', '검토 실행 저장소에 연결할 수 없습니다.')
    if not result.available:
        return result
    # Worker liveness is never cached: a stopped thread must immediately hide START.
    return worker_readiness(settings, getattr(request.app.state, 'extraction_worker', None))


def extraction_available(request):
    return extraction_readiness(request).available


def get_list_service(state):
    runtime = get_case_runtime(state)
    with state.service_lock:
        service = getattr(state, 'work_case_list_service', None)
        if service is None:
            settings = state.settings
            client = make_server_client(settings, http1_only=True)
            service = WorkCaseListService(WorkCaseListRepository(client,
                                         extraction_fields=True, column_missing=runtime.cached_column_missing),
                                         CursorCodec(settings.google_token_encryption_key.get_secret_value()))
            state.work_case_list_service = service
    return service


EXAMPLE = {
    'data': {
        'items': [{
            'case_id': '4c1b2687-c86b-4aca-986f-3a5aa1aa1001',
            'initial_mail_id': '4c1b2687-c86b-4aca-986f-3a5aa1aa2001',
            'title': 'sales@example.com', 'sender': 'sales@example.com', 'sender_name': None, 'requester_name': None,
            'received_at': '2026-10-29T06:00:00.000000Z', 'status': 'Waiting',
            'processing_step': 'PENDING_START', 'input_revision': 1, 'archived_at': None,
            'item_name': None, 'attachment_count': 1, 'hold': None,
            'step_detail': {'code': 'PENDING_START', 'message': '원문 확인 후 검토 시작'},
            'reply_summary': {'requested_supplier_count': 0, 'responded_supplier_count': 0, 'reply_rate': None},
            'allowed_actions': ['VIEW_ORIGINAL', 'ACCEPT_INCREASE'],
            'disabled_actions': [{'action': 'START', 'reason_code': 'EXTRACTION_DISABLED',
                                  'message': '서버의 검토 실행 설정이 꺼져 있습니다.'}],
        }],
        'next_cursor': None, 'counts': {'Waiting': 1, 'Running': 0, 'Complete': 0, 'total': 1},
        'intake_pending': 0,
    },
    'meta': {'request_id': 'req-example-01'},
}


@router.get('', response_model=Envelope[WorkCaseList], summary='업무 큐 목록·검색·상태별 건수',
            responses={200: {'content': {'application/json': {'example': EXAMPLE}}}})
def list_work_cases(
    request: Request, principal=Depends(require_google_user),
    status: QueueFilter | None = None,
    archived: bool = False,
    q: Annotated[str, Query(max_length=200)] = '',
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    cursor: Annotated[str | None, Query(max_length=2048)] = None,
):
    """Read only: never starts extraction or changes work state.

    Reset cursor when status, archive filter or search changes. Counts ignore only
    status, not search/archive. Counts and the Waiting/Running/Complete filters use
    the queue tabs: Waiting includes Confirm, Running includes Pending, so the three
    counters add up to total. status=Pending or Confirm returns only that status.
    sender_name is null when the source column or its value is unavailable.
    Actions may be temporarily disabled while integration checks finish.
    reply_summary counts distinct suppliers from SENT RFQs and verified quote
    replies in the current request round. Sidebar totals belong to the frontend.
    """
    service = get_list_service(request.app.state)
    runtime = get_runtime_repository(request)
    settings = request.app.state.settings
    fallback = configuration_readiness(settings)
    if fallback.available:
        fallback = worker_readiness(settings, getattr(request.app.state, 'extraction_worker', None))
    if fallback.available:
        fallback = StartReadiness('PROCESSING_UNAVAILABLE', '검토 실행 준비 상태를 확인 중입니다.')

    def probe_actions():
        start = extraction_readiness(request, runtime)
        resume = repository_resume_readiness(runtime) if start.available else start
        return start, resume, case_changes_ready(runtime)

    # One in-flight readiness check per app. A slow probe can outlive a list response.
    with request.app.state.service_lock:
        probe = getattr(request.app.state, 'work_case_list_readiness', None)
        if probe is None or probe.done():
            probe = _LIST_READINESS.submit(probe_actions)
            probe.add_done_callback(lambda done: _remember_readiness(request.app.state, done))
            request.app.state.work_case_list_readiness = probe

    def ready_for_actions():
        try:
            start, resume, changes_ready = probe.result(timeout=0.025)
        except TimeoutError:
            # 확인이 늦으면 직전에 끝난 확인 결과를 쓴다(있으면). 그래야 버튼이 주기적으로 꺼졌다 켜지지 않는다
            last = getattr(request.app.state, 'work_case_list_readiness_done', None)
            if last is None or time.monotonic() - last[0] > READINESS_REUSE_SECONDS:
                return fallback, fallback, False
            start, resume, changes_ready = last[1]
        if start.available:
            live = worker_readiness(settings, getattr(request.app.state, 'extraction_worker', None))
            if not live.available:
                return live, live, changes_ready
        return start, resume, changes_ready

    result = service.list(principal.user_id, status=status, archived=archived, q=q, limit=limit, cursor=cursor,
                          start_readiness=fallback, action_readiness=ready_for_actions)
    return api_json(result.model_dump(mode='json'), headers={'Cache-Control': 'private, no-store'})


class CaseActionRequest(StrictModel):
    expected_revision: int = Field(strict=True, ge=1)


class StartRequest(CaseActionRequest):
    quantity: str | None = Field(default=None, pattern=r'^[0-9]{1,14}(\.[0-9]{1,6})?$')
    unit: str | None = Field(default=None, min_length=1, max_length=50)
    due_date: date | None = None

    @model_validator(mode='after')
    def validate_quantity_pair(self):
        if (self.quantity is None) != (self.unit is None):
            raise ValueError('quantity and unit must be supplied together')
        if self.quantity is not None:
            quantity = Decimal(self.quantity)
            if quantity <= 0 or not self.unit.strip():
                raise ValueError('positive quantity and nonblank unit required')
            self.quantity = format(quantity, 'f').rstrip('0').rstrip('.') if '.' in self.quantity else str(int(quantity))
            self.unit = self.unit.strip()
        return self


class ResumeRequest(CaseActionRequest):
    reason: Literal['RETRY_FAILED', 'RESUME_HOLD', 'REOPEN_REVIEW', 'REOPEN_ACCEPTED']


def get_runtime_repository(request: Request):
    return get_case_runtime(request.app.state)


def get_case_runtime(state):
    with state.service_lock:
        repo = getattr(state, 'case_runtime_repository', None)
        if repo is None:
            repo = runtime_repository(make_server_client(state.settings), state.settings)
            state.case_runtime_repository = repo
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


def load_action_case(repo, owner, case_id):
    try:
        rows = (repo.client.table('work_cases')
            .select('id,initial_mail_id,status,processing_step,input_revision,item_name,specification,quantity::text,unit,'
                    'due_date,mandatory_conditions_jsonb,policy_jsonb,decision,archived_at,current_batch_id')
            .eq('owner_user_id', str(owner)).eq('id', str(case_id)).limit(1).execute().data)
    except Exception:
        raise ServiceError('PROCESSING_UNAVAILABLE', '업무를 조회할 수 없습니다.', 503) from None
    return stored_case_row(rows, ('id', 'initial_mail_id', 'status', 'processing_step', 'input_revision',
                                 'item_name', 'specification', 'quantity', 'unit', 'decision', 'archived_at', 'current_batch_id'))


def action_response(request, principal, repo, case_id, payload, key, action):
    replay = None
    conditions = None
    if action == 'START':
        validate_action_key(key)
        # Use a read-only receipt lookup only if already available in Supabase.
        # Otherwise let the existing transactional submit RPC handle replay and
        # revision/state guards; Python must not reject an already-started replay.
        if isinstance(payload, StartRequest):
            conditions = payload.model_dump(mode='json', exclude={'expected_revision'}, exclude_none=True)
        options = {'conditions': conditions} if conditions else {}
        replay = repo.start_receipt(principal.user_id, case_id, payload.expected_revision, key, **options)
        if replay is None:
            load_action_case(repo, principal.user_id, case_id)
            extraction_readiness(request, repo, fresh=True).require()
    status, response = WorkCaseActions(repo, extraction_enabled=True).submit(
        principal.user_id, case_id, payload.expected_revision, action, key, replay_result=replay, conditions=conditions)
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
def start_case(case_id: UUID, payload: StartRequest, request: Request,
               principal=Depends(require_google_user), repo=Depends(get_runtime_repository),
               idempotency_key: Annotated[str | None, Header(alias='Idempotency-Key')] = None):
    return action_response(request, principal, repo, case_id, payload, idempotency_key, 'START')


@router.post('/{case_id}/resume', status_code=202, response_model=Envelope[StartedCase],
             summary='추출 보류 업무 재개: EXTRACTING부터 백그라운드 실행')
def resume_case(case_id: UUID, payload: ResumeRequest, request: Request,
                principal=Depends(require_google_user), repo=Depends(get_runtime_repository),
                idempotency_key: Annotated[str | None, Header(alias='Idempotency-Key')] = None):
    """Resume a graph job after the durable transaction validates the reason.

    The durable transaction replays before state/revision checks, then checks
    ownership, latest extraction HOLD, unresolved sends and absence of live jobs.
    No model invocation or mail sending occurs inside this HTTP request.
    """
    validate_action_key(idempotency_key)
    # Resume never deletes its case: check visibility before inspecting storage
    # readiness so foreign/missing resources consistently remain 404.
    load_action_case(repo, principal.user_id, case_id)
    if payload.reason not in {'RESUME_HOLD', 'RETRY_FAILED', 'REOPEN_REVIEW'}:
        raise ServiceError('ACTION_NOT_ALLOWED', '지원하지 않는 재개 사유입니다.', 409, retryable=False)
    replay = repo.resume_receipt(principal.user_id, case_id, payload.expected_revision,
                                 payload.reason, idempotency_key)
    if replay is None:
        # Visibility check is safe before readiness. Do not reject a retry on
        # state/revision here: a concurrent transaction may already have accepted it.
        resume_readiness(request, repo, fresh=True).require()
    status, response = WorkCaseActions(repo, extraction_enabled=True).submit(
        principal.user_id, case_id, payload.expected_revision, 'RESUME', idempotency_key,
        replay_result=replay, reason=payload.reason)
    worker = getattr(request.app.state, 'extraction_worker', None)
    if worker is not None:
        worker.wake.set()
    return JSONResponse(response, status_code=status, headers={
        'X-Request-ID': response['meta']['request_id'], 'Cache-Control': 'private, no-store'})


@router.post('/{case_id}/decisions', summary='검토 판단 저장')
def save_decision(case_id: UUID, payload: DecisionRequest,
                  principal=Depends(require_google_user), repo=Depends(get_runtime_repository),
                  idempotency_key: Annotated[str | None, Header(alias='Idempotency-Key')] = None):
    return submit_case_change(repo, principal.user_id, case_id, 'DECISION', payload, idempotency_key)


@router.post('/{case_id}/archive', summary='업무 보관')
def archive_case(case_id: UUID, payload: RevisionRequest,
                 principal=Depends(require_google_user), repo=Depends(get_runtime_repository),
                 idempotency_key: Annotated[str | None, Header(alias='Idempotency-Key')] = None):
    return submit_case_change(repo, principal.user_id, case_id, 'ARCHIVE', payload, idempotency_key)


@router.post('/{case_id}/restore', summary='업무 복원')
def restore_case(case_id: UUID, payload: RevisionRequest,
                 principal=Depends(require_google_user), repo=Depends(get_runtime_repository),
                 idempotency_key: Annotated[str | None, Header(alias='Idempotency-Key')] = None):
    return submit_case_change(repo, principal.user_id, case_id, 'RESTORE', payload, idempotency_key)


def case_hold(repo, owner, row):
    hold = (HoldReasonRepository(repo.client).for_cases(owner, [row]).get(str(row['id']))
            if row['processing_step'] == 'ON_HOLD' else None)
    return hold.model_dump() if hold is not None else None


PROGRESS_ERROR_CODES = frozenset({
    'EXTRACTION_FAILED', 'EXTRACTION_TIME_LIMIT', 'WORKER_INTERRUPTED', 'INPUT_REVISION_CHANGED',
    'RFQ_PREPARATION_FAILED', 'NO_REGISTERED_SUPPLIER_EVIDENCE', 'NO_SUITABLE_CANDIDATES',
    'SEND_POLICY_REQUIRED', 'SEND_RESULT_UNKNOWN', 'SETTINGS_CHANGED', 'RFQ_CONDITIONS_REQUIRED',
    'GRAPH_FAILED', 'GRAPH_TIME_LIMIT', 'GRAPH_ORPHANED'})
PROGRESS_WAITING_STEPS = frozenset({'WAITING_REPLY', 'WAITING_APPROVAL', 'AWAITING_APPROVAL', 'NEEDS_INFO',
                                    'ON_HOLD', 'SEND_UNKNOWN', 'SELECTING_CANDIDATES'})


def progress_waiting_reason(step):
    return STEP_MESSAGES.get(step) if step in PROGRESS_WAITING_STEPS else None


def progress_error(step, code):
    """API-03 error. ON_HOLD already explains itself through `hold`, so an unknown
    stored code there is not re-labelled as EXTRACTION_FAILED."""
    if not code:
        return None
    if isinstance(code, str) and code in PROGRESS_ERROR_CODES:
        return {'code': code}
    return None if step == 'ON_HOLD' else {'code': 'EXTRACTION_FAILED'}


@router.get('/{case_id}/messages', summary='업무의 송수신 메일·회신 현황 조회')
def case_messages(case_id: UUID, request: Request, principal=Depends(require_google_user),
                  cursor: Annotated[str | None, Query()] = None,
                  limit: Annotated[int, Query(ge=1, le=100)] = 25):
    """API-10. Reuses the list service's client, cursor key and reply facts RPC."""
    from app.features.work_cases.infrastructure.messages_repository import list_case_messages
    service = get_list_service(request.app.state)
    return api_json(list_case_messages(service.repo.client, service.codec, principal.user_id, case_id,
                                       cursor=cursor, limit=limit, reply_summaries=service.repo.reply_summaries),
                    headers={'Cache-Control': 'private, no-store'})


@router.get('/{case_id}/progress', summary='저장된 검토 단계 조회')
def case_progress(case_id: UUID, request: Request, principal=Depends(require_google_user), repo=Depends(get_runtime_repository)):
    try:
        rows = (repo.client.table('work_cases')
                .select('id,status,processing_step,input_revision,error_jsonb,updated_at,current_batch_id,archived_at,decision')
                .eq('owner_user_id', principal.user_id).eq('id', str(case_id)).limit(1).execute().data)
    except Exception:
        raise ServiceError('PROCESSING_UNAVAILABLE', '처리 단계를 조회할 수 없습니다.', 503) from None
    row = stored_case_row(rows, ('id', 'status', 'processing_step', 'input_revision', 'current_batch_id'))
    error = row.get('error_jsonb')
    if error is not None and not isinstance(error, dict):
        raise ServiceError('PROCESSING_UNAVAILABLE', '저장된 오류 상태를 확인할 수 없습니다.', 503)
    code = (error or {}).get('code')
    try:
        updated_at = iso_z(utc_time(row['updated_at'])) if row.get('updated_at') else None
    except (ValueError, AttributeError, TypeError):
        raise ServiceError('PROCESSING_UNAVAILABLE', '저장된 시각 형식을 확인할 수 없습니다.', 503) from None
    owner = principal.user_id
    step = row['processing_step']
    roles = []
    try:
        if row['current_batch_id']:
            roles = (repo.client.table('agent_runs').select('agent_role')
                     .eq('owner_user_id', owner).eq('work_case_id', str(case_id))
                     .eq('review_batch_id', row['current_batch_id']).eq('input_revision', row['input_revision'])
                     .eq('status', 'RUNNING').order('id').limit(25).execute().data)
        history = (repo.client.table('workflow_logs').select('id,created_at')
                   .eq('owner_user_id', owner).eq('work_case_id', str(case_id))
                   .eq('input_revision', row['input_revision']).eq('event_type', 'REVIEW_STEP_CHANGED')
                   .order('created_at', desc=True).order('id', desc=True).limit(25).execute().data)
    except Exception:
        raise ServiceError('PROCESSING_UNAVAILABLE', '검토 실행 이력을 조회할 수 없습니다.', 503) from None
    resumable = False
    if step in {'ON_HOLD', 'FAILED', 'REVIEW_READY'}:
        readiness = resume_readiness(request, repo)
        actions, _ = case_actions(row, readiness, readiness)
        resumable = 'RESUME' in actions and not ResumeEligibilityRepository(repo.client).blocks(owner, [row])
    return api_json({'case_id': row['id'], 'status': row['status'], 'processing_step': row['processing_step'],
                     'hold': case_hold(repo, owner, row),
                     'running_roles': sorted({run['agent_role'] for run in roles}),
                     'step_history': [{'id': event['id'], 'event_type': 'REVIEW_STEP_CHANGED',
                                       'created_at': iso_z(utc_time(event['created_at']))} for event in history],
                     'waiting_reason': progress_waiting_reason(step),
                     'can_resume': resumable,
                     'input_revision': row['input_revision'], 'updated_at': updated_at,
                     'batch_id': row.get('current_batch_id'), 'progress_url': f'/api/v1/work-cases/{case_id}/progress',
                     'error': progress_error(step, code)})


@router.get('/{case_id}', summary='업무의 저장 상태 조회: 실행 없음')
def get_case(case_id: UUID, request: Request, principal=Depends(require_google_user), repo=Depends(get_runtime_repository)):
    row = load_action_case(repo, principal.user_id, case_id)
    related_cases = repo.related_case_ids(principal.user_id, case_id, row['initial_mail_id'])
    actions, disabled = case_actions(row, extraction_readiness(request, repo), resume_readiness(request, repo),
                                     case_changes_ready=case_changes_ready(repo))
    if 'RESUME' in actions:
        block = ResumeEligibilityRepository(repo.client).blocks(principal.user_id, [row]).get(str(case_id))
        if block:
            actions.remove('RESUME')
            disabled.append({'action': 'RESUME', 'reason_code': block[0], 'message': block[1]})
    if row.get('processing_step') in {'CANDIDATES', 'SELECTING_CANDIDATES'}:
        from app.features.work_cases.infrastructure.candidate_repository import CandidateRepository
        from app.features.work_cases.application.candidates import apply_candidate_action
        block = CandidateRepository(repo.client).blocks(principal.user_id, [row]).get(str(case_id))
        apply_candidate_action(row, actions, disabled, block)
    batch_id = row.pop('current_batch_id')
    # API-02 carries the stored goal conditions and policy (08 §5).
    row['mandatory_conditions'] = row.pop('mandatory_conditions_jsonb', None)
    row['policy'] = row.pop('policy_jsonb', None)
    return api_json({'case_id': row.pop('id'), **row,
                    'batch_id': batch_id, 'progress_url': f'/api/v1/work-cases/{case_id}/progress',
                    'allowed_actions': actions, 'disabled_actions': disabled, 'related_cases': related_cases,
                    'hold': case_hold(repo, principal.user_id, {**row, 'id': str(case_id), 'current_batch_id': batch_id})})
