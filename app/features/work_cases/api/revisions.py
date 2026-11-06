"""Authenticated revision requests with a single atomic persistence boundary."""
import hashlib
import json
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import JSONResponse
from pydantic import Field

from app.core.errors import ServiceError
from app.core.request_context import current_request_id
from app.features.mailboxes.api.router import require_google_user
from app.features.work_cases.api.decisions import RevisionRequest
from app.features.work_cases.application.actions import validate_action_key
from app.features.work_cases.infrastructure.runtime_repository import CaseRuntimeRepository
from app.shared.infrastructure.db.client import make_server_client

router = APIRouter(prefix='/api/v1', tags=['Work revisions'])
IdempotencyKey = Annotated[str | None, Header(alias='Idempotency-Key')]


class ConditionsRevision(RevisionRequest):
    changes: dict = Field(min_length=1)
    reason: str | None = None
    evidence_refs: list = Field(default_factory=list)


class QuoteCorrection(RevisionRequest):
    source_quote_id: UUID | None = None
    corrections: dict = Field(min_length=1)
    reason: str = Field(min_length=1)
    evidence_refs: list = Field(default_factory=list)


class DraftRevision(RevisionRequest):
    expected_snapshot_hash: str = Field(min_length=1)
    recipient: str = Field(min_length=1)
    subject: str = Field(min_length=1)
    body_text: str
    edit_reason: str = Field(min_length=1)


def submit(request, owner, target, operation, payload, key, status):
    validate_action_key(key)
    body = payload.model_dump(mode='json')
    digest = hashlib.sha256(json.dumps(body, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    repo = CaseRuntimeRepository(make_server_client(request.app.state.settings))
    try:
        result = repo.call('submit_work_revision', p_owner=str(owner), p_target=str(target),
                           p_operation=operation, p_body=body, p_key=key, p_hash=digest,
                           p_request_id=current_request_id())
    except ServiceError as error:
        if error.code != 'EXTRACTION_SCHEMA_REQUIRED':
            raise
        raise ServiceError('WORK_REVISION_INTEGRATION_REQUIRED', '수정 저장 기능이 아직 준비되지 않았습니다.',
                           503, retryable=False) from None
    if isinstance(result, dict) and result.get('kind') in {'ACCEPTED', 'REPLAY'} and result.get('http_status') == status:
        response = result.get('response')
        if isinstance(response, dict) and isinstance(response.get('data'), dict):
            meta = response.get('meta')
            if isinstance(meta, dict) and isinstance(meta.get('request_id'), str):
                return JSONResponse(response, status_code=status, headers={
                    'Cache-Control': 'private, no-store', 'X-Request-ID': meta['request_id']})
    raise ServiceError('PROCESSING_UNAVAILABLE', '저장된 수정 결과를 확인할 수 없습니다.', 503)


@router.patch('/work-cases/{case_id}/conditions', status_code=202)
def edit_conditions(case_id: UUID, payload: ConditionsRevision, request: Request,
                    principal=Depends(require_google_user), key: IdempotencyKey = None):
    return submit(request, principal.user_id, case_id, 'EDIT_CONDITIONS', payload, key, 202)


@router.post('/quote-versions/{quote_id}/corrections', status_code=202)
def correct_quote(quote_id: UUID, payload: QuoteCorrection, request: Request,
                  principal=Depends(require_google_user), key: IdempotencyKey = None):
    if payload.source_quote_id is not None and payload.source_quote_id != quote_id:
        raise ServiceError('INVALID_CONDITIONS', '정정 대상 견적이 요청 경로와 일치해야 합니다.', 422)
    payload = payload.model_copy(update={'source_quote_id': quote_id})
    return submit(request, principal.user_id, quote_id, 'CORRECT_QUOTE', payload, key, 202)


@router.post('/mail-drafts/{draft_id}/revisions', status_code=201)
def revise_draft(draft_id: UUID, payload: DraftRevision, request: Request,
                 principal=Depends(require_google_user), key: IdempotencyKey = None):
    return submit(request, principal.user_id, draft_id, 'EDIT_DRAFT', payload, key, 201)
