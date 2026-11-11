"""Accept deletes the case; start queues extraction. No LLM call in the HTTP request."""
import hashlib
import json
from app.core.errors import ServiceError
from app.core.idempotency import valid_idempotency_key
from app.core.request_context import current_request_id
from app.api.responses import Envelope
from app.features.work_cases.domain.actions import DeletedCase, StartedCase


def validate_action_key(key):
    if not valid_idempotency_key(key):
        raise ServiceError('IDEMPOTENCY_KEY_REQUIRED', '유효한 Idempotency-Key가 필요합니다.', 400, retryable=False)


def revision_body_hash(revision, conditions=None):
    body = json.dumps({'expected_revision': revision, **(conditions or {})},
                      separators=(',', ':'), sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(body.encode()).hexdigest()


def resume_body_hash(revision, reason):
    body = json.dumps({'expected_revision': revision, 'reason': reason}, separators=(',', ':'), sort_keys=True)
    return hashlib.sha256(body.encode()).hexdigest()


class WorkCaseActions:
    def __init__(self, repo, *, extraction_enabled=False):
        self.repo, self.extraction_enabled = repo, extraction_enabled

    def submit(self, owner, case_id, revision, action, key, *, replay_result=None, reason=None, conditions=None):
        if action not in {'START', 'RESUME', 'ACCEPT_INCREASE'}:
            raise ServiceError('ACTION_NOT_ALLOWED', '허용하지 않은 업무 동작입니다.', 409, retryable=False)
        validate_action_key(key)
        if action == 'RESUME' and reason not in {'RESUME_HOLD', 'RETRY_FAILED', 'REOPEN_REVIEW'}:
            raise ServiceError('ACTION_NOT_ALLOWED', '지원하지 않는 재개 사유입니다.', 409, retryable=False)
        if replay_result is not None:
            if action not in {'START', 'RESUME'} or not isinstance(replay_result, dict) or replay_result.get('kind') != 'REPLAY':
                raise ServiceError('PROCESSING_UNAVAILABLE', '저장된 요청 결과를 확인할 수 없습니다.', 503)
            result = replay_result
        else:
            if action in {'START', 'RESUME'} and not self.extraction_enabled:
                raise ServiceError('PROCESSING_UNAVAILABLE', '검토 실행 연결을 준비 중입니다.', 503, retryable=False)
            if action == 'RESUME':
                result = self.repo.resume(str(owner), str(case_id), revision, reason, key,
                                          resume_body_hash(revision, reason), current_request_id())
            else:
                options = {'conditions': conditions} if conditions else {}
                result = self.repo.action(str(owner), str(case_id), revision, action, key,
                                          revision_body_hash(revision, conditions), current_request_id(), **options)
        # The DB must replay before lookup, even if the case was already deleted.
        if not isinstance(result, dict) or result.get('kind') not in {'ACCEPTED', 'REPLAY'}:
            raise ServiceError('PROCESSING_UNAVAILABLE', '업무 접수를 확인할 수 없습니다.', 503)
        expected_status = 202 if action in {'START', 'RESUME'} else 200
        if result.get('http_status') != expected_status or not isinstance(result.get('response'), dict):
            raise ServiceError('PROCESSING_UNAVAILABLE', '저장된 응답을 확인할 수 없습니다.', 503)
        response = result['response']
        data, meta = response.get('data'), response.get('meta')
        if not isinstance(data, dict) or not isinstance(meta, dict):
            raise ServiceError('PROCESSING_UNAVAILABLE', '저장된 응답 구조를 확인할 수 없습니다.', 503)
        if str(data.get('case_id')) != str(case_id) or not isinstance(meta.get('request_id'), str):
            raise ServiceError('PROCESSING_UNAVAILABLE', '저장된 응답 범위를 확인할 수 없습니다.', 503)
        if action == 'ACCEPT_INCREASE' and data.get('deleted') is not True:
            raise ServiceError('PROCESSING_UNAVAILABLE', '수용 삭제 결과를 확인할 수 없습니다.', 503)
        if action in {'START', 'RESUME'} and (data.get('input_revision') != revision or not data.get('batch_id')):
            raise ServiceError('PROCESSING_UNAVAILABLE', '검토 실행 접수를 확인할 수 없습니다.', 503)
        try:
            model = StartedCase if action in {'START', 'RESUME'} else DeletedCase
            response = Envelope[model].model_validate(response).model_dump(mode='json')
            if action in {'START', 'RESUME'} and response['data']['progress_url'] != f'/api/v1/work-cases/{case_id}/progress':
                raise ValueError('Invalid progress route')
        except ValueError:
            raise ServiceError('PROCESSING_UNAVAILABLE', '저장된 응답 계약을 확인할 수 없습니다.', 503) from None
        return expected_status, response
