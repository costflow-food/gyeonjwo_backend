"""API-15 and archive/restore request contracts."""
import hashlib
import json
from typing import Literal
from uuid import UUID

from fastapi.responses import JSONResponse
from pydantic import Field, model_validator

from app.core.errors import ServiceError
from app.core.request_context import current_request_id
from app.features.work_cases.application.actions import validate_action_key
from app.shared.domain.schemas import StrictModel


class RevisionRequest(StrictModel):
    expected_revision: int = Field(strict=True, ge=1)


class DecisionRequest(RevisionRequest):
    decision: Literal['SELECT_PLAN', 'HOLD', 'CONFIRM_REVIEW']
    review_revision: int = Field(strict=True, ge=1)
    plan_id: str | None = Field(default=None, min_length=1)
    note: str | None = Field(default=None, max_length=2000)

    @model_validator(mode='after')
    def validate_fields(self):
        if self.decision == 'SELECT_PLAN' and not self.plan_id:
            raise ValueError('SELECT_PLAN requires plan_id')
        if self.decision != 'SELECT_PLAN' and self.plan_id is not None:
            raise ValueError('plan_id is only allowed for SELECT_PLAN')
        if self.decision != 'HOLD' and self.note is not None:
            raise ValueError('note is only allowed for HOLD')
        return self


def case_changes_ready(repo):
    try:
        return repo.case_changes_available()
    except ServiceError:
        return False


def submit_case_change(repo, owner, case_id: UUID, operation, payload, key):
    validate_action_key(key)
    body = payload.model_dump(mode='json')
    body_hash = hashlib.sha256(
        json.dumps(body, sort_keys=True, separators=(',', ':')).encode()
    ).hexdigest()
    result = repo.case_change_receipt(owner, case_id, operation, key, body_hash)
    if isinstance(result, dict) and result.get('kind') == 'NONE':
        if not case_changes_ready(repo):
            raise ServiceError('CASE_CHANGE_INTEGRATION_REQUIRED', '업무 변경 기능이 아직 준비되지 않았습니다.',
                               503, retryable=False)
        result = repo.submit_case_change(
            owner, case_id, operation, body, key, body_hash, current_request_id())
    if isinstance(result, dict) and result.get('kind') in {'ACCEPTED', 'REPLAY'} and result.get('http_status') == 200:
        response = result.get('response')
        if isinstance(response, dict):
            data, meta = response.get('data'), response.get('meta')
            if (isinstance(data, dict) and data.get('case_id') == str(case_id)
                    and isinstance(meta, dict) and isinstance(meta.get('request_id'), str)):
                return JSONResponse(response, headers={
                    'X-Request-ID': meta['request_id'], 'Cache-Control': 'private, no-store'})
    raise ServiceError('PROCESSING_UNAVAILABLE', '저장된 업무 변경 결과를 확인할 수 없습니다.', 503)
