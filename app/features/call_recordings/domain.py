"""통화 녹음 업로드(KAN-248) 계약.

규칙(2026-11-11 결정)
- 파일 1개 최대 50MB, 업무 1건당 진행 중·완료 녹음 최대 10개(실패는 세지 않음)
- 업무를 수용(삭제)하면 녹음 행은 DB 에서 함께 지워지고 Storage 파일은 삭제 대기열이 지운다
- 음성 파일은 견적 추출(①)이 끝나면 지운다. 전사문은 업무가 남아 있는 동안 보관한다(2026-11-12 결정)
- 업로드에 실패한(기한 만료·크기 불일치) 녹음은 실패 2시간 뒤 파일만 지우고 행은 남긴다(2026-11-12 결정)
- 오류 코드는 통화 녹음 전용 `CALL_RECORDING_*` 를 쓴다(멱등 공통 코드만 예외)
- 녹음은 견적 요청에 대한 거래처 회신이다: 이 업무에서 견적 요청(RFQ)을 보낸 거래처(supplier_id)가 필수다.
  보낸 거래처인지는 DB 가 업무 잠금 안에서 판단한다(CALL_RECORDING_SUPPLIER_NOT_ALLOWED)
DB call_recording_health 계약 버전과 값을 맞춘다.
"""
from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel

from app.shared.domain.schemas import StrictModel

BUCKET = 'call-recordings'
MAX_BYTES = 52_428_800
MAX_PER_CASE = 10
CONTRACT_VERSION = 3
# 확장자 → 업로드할 때 보낼 Content-Type. 버킷 허용 MIME 과 같은 목록(DB internal.call_recording_content_type).
CONTENT_TYPES = {
    'm4a': 'audio/mp4', 'mp3': 'audio/mpeg', 'wav': 'audio/wav', 'aac': 'audio/aac', 'amr': 'audio/amr',
    '3gp': 'audio/3gpp', 'ogg': 'audio/ogg', 'opus': 'audio/opus', 'flac': 'audio/flac',
}

# 통화 녹음 오류 규칙: 코드 → (HTTP 상태, 안내 문구, 재시도 가능, 다음 행동)
ERRORS = {
    'CALL_RECORDING_INVALID_REQUEST': (422, '녹음 파일 정보가 올바르지 않습니다.', False, None),
    'CALL_RECORDING_UNSUPPORTED_TYPE': (415, '지원하지 않는 녹음 파일 형식입니다.', False, None),
    'CALL_RECORDING_TOO_LARGE': (413, '녹음 파일은 50MB까지 올릴 수 있습니다.', False, None),
    'CALL_RECORDING_CASE_NOT_FOUND': (404, '업무를 찾을 수 없습니다.', False, 'REFRESH_CASE'),
    'CALL_RECORDING_NOT_FOUND': (404, '녹음을 찾을 수 없습니다.', False, 'REFRESH_RECORDINGS'),
    'CALL_RECORDING_SUPPLIER_NOT_ALLOWED': (422, '견적을 요청한 거래처만 고를 수 있습니다.', False, 'REFRESH_CASE'),
    'CALL_RECORDING_LIMIT_EXCEEDED': (409, '업무 하나에 녹음은 10개까지 올릴 수 있습니다.', False, None),
    'CALL_RECORDING_UPLOAD_INCOMPLETE': (409, '파일 업로드가 아직 끝나지 않았습니다.', False, 'UPLOAD_FILE'),
    'CALL_RECORDING_UPLOAD_EXPIRED': (410, '업로드 가능 시간이 지났습니다. 다시 추가해 주세요.', False, 'CREATE_RECORDING'),
    'CALL_RECORDING_SIZE_MISMATCH': (409, '올린 파일 크기가 신청한 크기와 다릅니다. 다시 추가해 주세요.', False, 'CREATE_RECORDING'),
    'CALL_RECORDING_STORAGE_UNAVAILABLE': (503, '녹음 저장소에 연결할 수 없습니다.', True, None),
    'CALL_RECORDING_UNAVAILABLE': (503, '통화 녹음 기능을 사용할 수 없습니다.', False, 'CONTACT_SERVER_ADMIN'),
    # 멱등 처리는 모든 변경 API 공통 코드를 그대로 쓴다.
    'IDEMPOTENCY_KEY_REUSED': (409, '같은 키에 다른 요청 본문을 사용할 수 없습니다.', False, None),
    'REQUEST_IN_PROGRESS': (409, '같은 요청이 처리 중입니다. 같은 키로 확인해 주세요.', False, None),
}

Status = Literal['UPLOADING', 'UPLOADED', 'FAILED']


class CreateRecordingRequest(StrictModel):
    supplier_id: UUID
    file_name: str
    extension: str
    size_bytes: int


class UploadTarget(BaseModel):
    method: Literal['PUT']
    url: str
    token: str
    path: str
    content_type: str
    expires_at: datetime


class CallRecording(BaseModel):
    recording_id: UUID
    case_id: UUID
    supplier_id: UUID
    file_name: str
    extension: str
    content_type: str
    size_bytes: int
    status: Status
    failure_reason: Literal['EXPIRED', 'SIZE_MISMATCH'] | None
    created_at: datetime
    uploaded_at: datetime | None
    stt_status: Literal['rec-pending', 'rec-processing', 'rec-completed', 'rec-failed'] | None = None


class CreatedRecording(CallRecording):
    upload: UploadTarget | None


class RecordingLimits(BaseModel):
    max_per_case: int
    max_bytes: int


class RecordingList(BaseModel):
    items: list[CallRecording]
    limits: RecordingLimits
