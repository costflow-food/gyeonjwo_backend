"""통화 녹음 업로드 API (KAN-248, 업로드·목록만). STT·재생·삭제는 범위 밖.

녹음은 견적 요청을 보낸 거래처의 회신으로 붙는다: 업로드 자리 요청에 supplier_id 가 필수다.
"""
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Request

from app.api.responses import Envelope, ErrorEnvelope, api_json
from app.core.request_context import current_request_id
from app.features.call_recordings.application import CallRecordingService
from app.features.call_recordings.domain import CallRecording, CreatedRecording, CreateRecordingRequest, RecordingList
from app.features.call_recordings.infrastructure import CallRecordingRepository, CallRecordingStorage
from app.features.mailboxes.api.router import require_google_user
from app.shared.infrastructure.db.client import make_server_client

router = APIRouter(prefix='/api/v1/work-cases', tags=['Call recordings'],
                   responses={status: {'model': ErrorEnvelope} for status in (410, 415)})
NO_STORE = {'Cache-Control': 'private, no-store'}


def get_call_recording_service(request: Request):
    state = request.app.state
    with state.service_lock:
        service = getattr(state, 'call_recording_service', None)
        if service is None:
            client = make_server_client(state.settings)
            service = CallRecordingService(CallRecordingRepository(client), CallRecordingStorage(client))
            state.call_recording_service = service
    return service


@router.post('/{case_id}/call-recordings', status_code=201, response_model=Envelope[CreatedRecording],
             summary='통화 녹음 업로드 자리 받기: 서명 URL 로 앱이 Storage 에 직접 PUT')
def create_call_recording(case_id: UUID, payload: CreateRecordingRequest,
                          principal=Depends(require_google_user), service=Depends(get_call_recording_service),
                          idempotency_key: Annotated[str | None, Header(alias='Idempotency-Key')] = None):
    """같은 Idempotency-Key 재요청은 녹음을 새로 만들지 않고, 업로드 중이면 새 서명 URL 을 다시 준다.

    `supplier_id` 는 이 업무에서 견적 요청을 보낸 거래처여야 한다. 아니면 CALL_RECORDING_SUPPLIER_NOT_ALLOWED.

    업로드: `PUT upload.url` (헤더 `Content-Type: upload.content_type`, 본문 = 파일). 덮어쓰기 불가.
    `upload.expires_at` 까지 올리고 완료 요청을 보내야 한다.
    """
    status, data = service.create(principal.user_id, case_id, payload, idempotency_key, current_request_id())
    return api_json(data, status=status, model=CreatedRecording, headers=NO_STORE)


@router.post('/{case_id}/call-recordings/{recording_id}/complete', response_model=Envelope[CallRecording],
             summary='통화 녹음 업로드 완료: Storage 의 실제 파일·크기를 확인')
def complete_call_recording(case_id: UUID, recording_id: UUID, request: Request,
                            principal=Depends(require_google_user), service=Depends(get_call_recording_service)):
    """반복 호출해도 결과가 같다. 파일이 아직 없으면 CALL_RECORDING_UPLOAD_INCOMPLETE.
    업로드가 끝나면 전사 워커를 깨운다(전사는 백그라운드에서 하고 이 응답은 기다리지 않는다)."""
    data = service.complete(principal.user_id, case_id, recording_id)
    if data.get('status') == 'UPLOADED':
        worker = getattr(request.app.state, 'call_transcription_worker', None)
        if worker is not None:
            worker.wake()
    return api_json(data, model=CallRecording, headers=NO_STORE)


@router.get('/{case_id}/call-recordings', response_model=Envelope[RecordingList], summary='업무의 통화 녹음 목록(최신순)')
def list_call_recordings(case_id: UUID, principal=Depends(require_google_user),
                         service=Depends(get_call_recording_service)):
    data = service.list(principal.user_id, case_id)
    return api_json(data, model=RecordingList, headers=NO_STORE)
