"""Latest cards 4/5: accept deletes; start queues extraction, no LLM in HTTP."""
import hashlib
import json
from app.core.errors import ServiceError
from app.core.request_context import current_request_id
from app.api.responses import Envelope
from app.features.work_cases.domain.actions import DeletedCase, StartedCase


class WorkCaseActions:
    def __init__(self, repo, *, extraction_enabled=False):
        self.repo, self.extraction_enabled = repo, extraction_enabled

    def submit(self, owner, case_id, revision, action, key):
        if not isinstance(key, str) or not 1 <= len(key) <= 200 or key.strip() != key or any(ord(c) < 32 for c in key):
            raise ServiceError('IDEMPOTENCY_KEY_REQUIRED', '유효한 Idempotency-Key가 필요합니다.', 400, retryable=False)
        if action == 'START' and not self.extraction_enabled:
            raise ServiceError('PROCESSING_UNAVAILABLE', '검토 실행 연결을 준비 중입니다.', 503, retryable=False)
        body = json.dumps({'expected_revision': revision}, separators=(',', ':'), sort_keys=True)
        result = self.repo.action(str(owner), str(case_id), revision, action, key,
                                  hashlib.sha256(body.encode()).hexdigest(), current_request_id())
        # The DB must replay before lookup, even if the case was already deleted.
        if not isinstance(result, dict) or result.get('kind') not in {'ACCEPTED', 'REPLAY'}:
            raise ServiceError('PROCESSING_UNAVAILABLE', '업무 접수를 확인할 수 없습니다.', 503)
        expected_status = 202 if action == 'START' else 200
        if result.get('http_status') != expected_status or not isinstance(result.get('response'), dict):
            raise ServiceError('PROCESSING_UNAVAILABLE', '저장된 응답을 확인할 수 없습니다.', 503)
        response = result['response']
        data = response.get('data', {})
        if str(data.get('case_id')) != str(case_id) or not isinstance(response.get('meta', {}).get('request_id'), str):
            raise ServiceError('PROCESSING_UNAVAILABLE', '저장된 응답 범위를 확인할 수 없습니다.', 503)
        if action == 'ACCEPT_INCREASE' and data.get('deleted') is not True:
            raise ServiceError('PROCESSING_UNAVAILABLE', '수용 삭제 결과를 확인할 수 없습니다.', 503)
        if action == 'START' and (data.get('input_revision') != revision or not data.get('batch_id')):
            raise ServiceError('PROCESSING_UNAVAILABLE', '검토 실행 접수를 확인할 수 없습니다.', 503)
        try:
            model = StartedCase if action == 'START' else DeletedCase
            response = Envelope[model].model_validate(response).model_dump(mode='json')
            if action == 'START' and response['data']['progress_url'] != f'/api/v1/work-cases/{case_id}/progress':
                raise ValueError('Invalid progress route')
        except ValueError:
            raise ServiceError('PROCESSING_UNAVAILABLE', '저장된 응답 계약을 확인할 수 없습니다.', 503) from None
        return expected_status, response
