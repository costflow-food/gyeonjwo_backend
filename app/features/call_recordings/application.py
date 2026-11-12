"""통화 녹음 업로드 흐름: 업로드 자리 → (앱이 Storage 에 직접 PUT) → 완료 확인 → 목록."""
from datetime import datetime, timezone
from uuid import uuid4

from app.core.errors import ServiceError
from app.core.idempotency import request_identity
from app.features.call_recordings.domain import CONTENT_TYPES, ERRORS, MAX_BYTES

PUBLIC_FIELDS = ('recording_id', 'case_id', 'supplier_id', 'file_name', 'extension', 'content_type', 'size_bytes',
                 'status', 'failure_reason', 'created_at', 'uploaded_at', 'stt_status')


def recording_error(code):
    status, message, retryable, next_action = ERRORS.get(code, ERRORS['CALL_RECORDING_UNAVAILABLE'])
    if code not in ERRORS:
        code = 'CALL_RECORDING_UNAVAILABLE'
    return ServiceError(code, message, status, retryable=retryable, next_action=next_action)


def _with_supplier(recording):
    """거래처가 없는 응답은 DB 가 아직 계약 2(거래처 추가분) 이전이라는 뜻이다: 기능 미준비로 닫는다."""
    if not isinstance(recording, dict) or not recording.get('supplier_id'):
        raise recording_error('CALL_RECORDING_UNAVAILABLE')
    return recording


def _public(recording):
    return {key: recording.get(key) for key in PUBLIC_FIELDS}


def _parse_time(value):
    parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def collection_path(case_id):
    return f'/api/v1/work-cases/{case_id}/call-recordings'


class CallRecordingService:
    def __init__(self, repo, storage, *, clock=lambda: datetime.now(timezone.utc), new_id=uuid4):
        self.repo, self.storage, self.clock, self.new_id = repo, storage, clock, new_id

    @staticmethod
    def validate(payload):
        """DB 와 같은 규칙을 먼저 확인해 전용 오류 코드로 돌려준다. 확장자는 소문자로 맞춘다."""
        name, extension, size = payload.file_name, payload.extension, payload.size_bytes
        if (not isinstance(name, str) or not 1 <= len(name) <= 200 or not name.strip()
                or any(ord(ch) < 32 or ord(ch) == 127 or ch in '/\\' for ch in name)
                or type(size) is not int or size < 1 or not isinstance(extension, str)):
            raise recording_error('CALL_RECORDING_INVALID_REQUEST')
        extension = extension.strip().lstrip('.').casefold()
        if extension not in CONTENT_TYPES:
            raise recording_error('CALL_RECORDING_UNSUPPORTED_TYPE')
        if size > MAX_BYTES:
            raise recording_error('CALL_RECORDING_TOO_LARGE')
        return name, extension, size

    def upload_target(self, recording):
        """업로드 중이고 기한 전일 때만 새 서명 URL 을 발급한다(URL 은 DB 에 저장하지 않음)."""
        if recording.get('status') != 'UPLOADING':
            return None
        expires_at = _parse_time(recording['upload_expires_at'])
        if self.clock() >= expires_at:
            return None
        try:
            signed = self.storage.signed_upload(recording['storage_path'])
        except ServiceError:
            # 파일이 이미 올라가 있으면 Storage 가 서명을 거절한다(덮어쓰기 불가). 그때는 업로드 자리 없이 돌려줘
            # 앱이 바로 완료 요청으로 넘어가게 한다. 파일이 없으면 저장소 장애이므로 그대로 올린다.
            if self.storage.object_size(recording['storage_path']) is None:
                raise
            return None
        return {'method': 'PUT', 'url': signed['signed_url'], 'token': signed['token'], 'path': signed['path'],
                'content_type': recording['content_type'], 'expires_at': recording['upload_expires_at']}

    def create(self, owner, case_id, payload, key, request_id):
        body = payload.model_dump(mode='json')
        identity = request_identity(str(owner), 'POST', collection_path(case_id), key, body)
        name, extension, size = self.validate(payload)
        result = self.repo.create(owner, case_id, self.new_id(), payload.supplier_id, name, extension, size,
                                  identity.key, identity.body_hash, request_id)
        kind = result.get('kind') if isinstance(result, dict) else None
        if kind == 'ERROR':
            raise recording_error(result.get('code'))
        if kind not in {'CREATED', 'REPLAY'}:
            raise ServiceError('PROCESSING_UNAVAILABLE', '녹음 업로드 자리를 만들지 못했습니다.', 503)
        recording = result.get('recording')
        if recording is None:
            # 같은 키의 녹음이 이미 업무 삭제로 사라졌다: 저장된 첫 응답만 돌려준다.
            data = dict(_with_supplier(result['response']['data']), upload=None)
        else:
            data = dict(_public(_with_supplier(recording)), upload=self.upload_target(recording))
        return int(result.get('http_status') or 201), data

    def complete(self, owner, case_id, recording_id):
        row = self.repo.find(owner, case_id, recording_id)
        size = None
        if row is not None and row.get('status') == 'UPLOADING':
            size = self.storage.object_size(row['storage_path'])
        result = self.repo.complete(owner, case_id, recording_id, size)
        if not isinstance(result, dict) or result.get('kind') not in {'OK', 'ERROR'}:
            raise ServiceError('PROCESSING_UNAVAILABLE', '녹음 업로드 완료를 확인하지 못했습니다.', 503)
        if result['kind'] == 'ERROR':
            raise recording_error(result.get('code'))
        return _with_supplier(result['recording'])

    def list(self, owner, case_id):
        result = self.repo.list(owner, case_id)
        if not isinstance(result, dict) or result.get('kind') not in {'OK', 'ERROR'}:
            raise ServiceError('PROCESSING_UNAVAILABLE', '녹음 목록을 불러오지 못했습니다.', 503)
        if result['kind'] == 'ERROR':
            raise recording_error(result.get('code'))
        return {'items': [_with_supplier(item) for item in result['items']], 'limits': result['limits']}
