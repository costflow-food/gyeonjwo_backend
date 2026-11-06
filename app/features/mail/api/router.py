import json
import logging
from typing import Annotated
from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response
from fastapi.security import HTTPAuthorizationCredentials
from starlette.concurrency import run_in_threadpool
from app.api.dependencies import bearer, require_member
from app.api.responses import api_json, Envelope
from app.core.errors import ServiceError
from app.features.mail.infrastructure.gmail import parse_notification
from app.shared.infrastructure.llm import JsonAgent
from app.features.mail.domain.schemas import Classification, MailClassificationRequest
from app.features.mail.application.classification import classify_or_raise
from app.features.mailboxes.api.router import get_mailbox_service
from app.features.mailboxes.api.router import require_google_user
from app.features.mail.application.attachments import fetch_gmail_attachment
from app.features.mail.infrastructure.attachments import AttachmentRepository
from app.shared.infrastructure.db.client import make_server_client
from uuid import UUID
from io import BytesIO
from fastapi.responses import StreamingResponse
from app.features.mail.domain.original import OriginalMail
from app.features.mail.application.original import OriginalMailService
from app.features.mail.infrastructure.original import OriginalMailRepository

router = APIRouter(tags=['mail'])
protected = [Depends(require_member)]
receive_logger = logging.getLogger('uvicorn.error.gmail_receive')


def get_original_mail_service(request: Request):
    return OriginalMailService(OriginalMailRepository(make_server_client(request.app.state.settings)))


@router.get('/api/v1/mail-messages/{mail_id}', response_model=Envelope[OriginalMail],
            summary='원문 모달: 저장된 텍스트 본문·첨부 메타 조회',
            responses={404: {'description': 'RESOURCE_NOT_FOUND: 없음 또는 소유 범위 밖'},
                       200: {'content': {'application/json': {'example': {
                           'data': {'mail_id': 'ad9081c0-91d7-49e4-8cf2-2f6215f16e00',
                                    'subject': '합성 단가 변경 요청', 'sender': 'sales@example.invalid',
                                    'recipients': ['buyer@example.invalid'], 'received_at': '2026-10-30T00:00:00Z',
                                    'body_text': '첨부 요청서를 확인해 주세요.',
                                    'attachments': [{'attachment_id': '3cead8be-16b7-596f-9bc4-c79dd4d0a5d4',
                                                     'filename': '요청서.pdf', 'mime_type': 'application/pdf', 'size_bytes': 1024}],
                                    'mail_classification': 'BUSINESS', 'source_type': 'GMAIL'},
                           'meta': {'request_id': 'req-synthetic-original'}}}}}})
def original_mail(mail_id: UUID, principal=Depends(require_google_user),
                  service=Depends(get_original_mail_service)):
    """Read only. Does not contact Gmail or start extraction/acknowledgement.

    attachment_id is server-generated and works with API-26. source_type maps to
    the stored mail provider. HTML source and provider attachment IDs are omitted.
    """
    result = service.get(principal.user_id, mail_id)
    return api_json(result.model_dump(mode='json'), headers={'Cache-Control': 'private, no-store'})


@router.get('/api/v1/mail-messages/{mail_id}/attachments/{attachment_id}/content',
            summary='소유 메일의 Gmail 원본 첨부 조회',
            responses={200: {'content': {'application/pdf': {}, 'application/octet-stream': {}}},
                       404: {'description': 'RESOURCE_NOT_FOUND'},
                       409: {'description': 'MAILBOX_RECONNECT_REQUIRED'},
                       502: {'description': 'SOURCE_READ_FAILED'}})
def attachment_content(mail_id: UUID, attachment_id: str, request: Request,
                       principal=Depends(require_google_user)):
    repo = AttachmentRepository(make_server_client(request.app.state.settings))
    result = fetch_gmail_attachment(repo, lambda: get_mailbox_service(request),
                                    principal.user_id, mail_id, attachment_id)
    # In-memory chunks only; never spool to a temporary file or persist the body.
    stream = BytesIO(result.content)
    return StreamingResponse(iter(lambda: stream.read(65536), b''), headers=result.headers())

@router.post('/api/v1/webhooks/gmail', status_code=204)
async def gmail_webhook(request: Request, credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)]):
    receive_logger.info('gmail push_received')
    settings = request.app.state.settings
    # A verified notification is durably queued even while the mailbox worker is not running.
    if (not settings.pubsub_audience or not settings.pubsub_service_account or not settings.gmail_subscription
            or not settings.google_token_encryption_key):
        receive_logger.warning('gmail push_rejected code=CONFIGURATION_REQUIRED')
        raise ServiceError('INGEST_UNAVAILABLE', '메일 수신 설정이 필요합니다.', 503)
    if not credentials:
        receive_logger.warning('gmail push_rejected code=UNAUTHORIZED_PUSH')
        raise ServiceError('UNAUTHORIZED_PUSH', 'Pub/Sub 인증이 필요합니다.', 401)
    try:
        await run_in_threadpool(request.app.state.push_verifier, credentials.credentials, settings)
    except ServiceError:
        receive_logger.warning('gmail push_rejected code=UNAUTHORIZED_PUSH')
        raise
    receive_logger.info('gmail push_authenticated')
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > 65536:
            receive_logger.warning('gmail push_rejected code=PAYLOAD_TOO_LARGE')
            raise ServiceError('INVALID_NOTIFICATION', '알림 본문이 너무 큽니다.', 400)
    try:
        payload = json.loads(raw)
    except (ValueError, TypeError):
        receive_logger.warning('gmail push_rejected code=INVALID_JSON')
        raise ServiceError('INVALID_NOTIFICATION', '잘못된 알림 JSON입니다.', 400) from None
    try:
        notification_id, email, history_id = parse_notification(payload, settings)
    except ServiceError:
        receive_logger.warning('gmail push_rejected code=INVALID_NOTIFICATION_OR_SUBSCRIPTION')
        raise
    mailbox = get_mailbox_service(request)
    try:
        await run_in_threadpool(mailbox.repo.enqueue_push, settings.gmail_subscription, notification_id, email, history_id)
    except Exception:
        receive_logger.warning('gmail push_enqueue_failed mode=connected')
        raise
    receive_logger.info('gmail push_enqueued mode=connected')
    worker = request.app.state.connected_mailbox_worker
    if worker:
        worker.wake.set()
    return Response(status_code=204)


@router.post('/api/v1/llm/classify-mail', dependencies=protected, deprecated=True, tags=['legacy-mail'])
def classify_mail(payload: MailClassificationRequest, request: Request):
    """Authenticated, bounded LLM call; does not create work or send mail."""
    settings = request.app.state.settings
    try:
        settings.require_classification()
    except RuntimeError:
        raise ServiceError('CONFIGURATION_REQUIRED', 'LLM 연결 설정이 필요합니다.', 503) from None
    result = classify_or_raise(JsonAgent(settings), payload.subject, payload.body)
    return api_json({'classification': {'TRUE': 'BUSINESS', 'FALSE': 'NON_BUSINESS', 'PENDING': 'UNCERTAIN'}[result.status],
                     'reason': result.reason, 'persisted': False})

