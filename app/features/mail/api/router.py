import json
import logging
from typing import Annotated
from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response
from fastapi.security import HTTPAuthorizationCredentials
from starlette.concurrency import run_in_threadpool
from app.api.dependencies import bearer, get_service, require_member
from app.api.responses import api_json
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

router = APIRouter(tags=['mail'])
protected = [Depends(require_member)]
receive_logger = logging.getLogger('uvicorn.error.gmail_receive')


@router.get('/api/v1/mail-messages/{mail_id}/attachments/{attachment_id}/content',
            summary='소유 메일의 Gmail 원본 첨부 조회',
            responses={200: {'content': {'application/pdf': {}, 'application/octet-stream': {}}},
                       404: {'description': 'RESOURCE_NOT_FOUND'},
                       409: {'description': 'MAILBOX_RECONNECT_REQUIRED'},
                       502: {'description': 'SOURCE_READ_FAILED'}})
def attachment_content(mail_id: UUID, attachment_id: str, request: Request,
                       principal=Depends(require_google_user), mailbox=Depends(get_mailbox_service)):
    repo = AttachmentRepository(make_server_client(request.app.state.settings))
    result = fetch_gmail_attachment(repo, mailbox, principal.user_id, mail_id, attachment_id)
    # In-memory chunks only; never spool to a temporary file or persist the body.
    stream = BytesIO(result.content)
    return StreamingResponse(iter(lambda: stream.read(65536), b''), headers=result.headers())

@router.post('/api/v1/webhooks/gmail', status_code=204)
async def gmail_webhook(request: Request, credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)]):
    receive_logger.info('gmail push_received')
    settings = request.app.state.settings
    # Pausing the worker must not send connected-mailbox pushes into the legacy DB path.
    # A verified notification can still be durably queued while processing is paused.
    connected = settings.google_mailbox_worker_enabled or (
        not settings.worker_enabled and bool(settings.google_token_encryption_key))
    if (not settings.pubsub_audience or not settings.pubsub_service_account or not settings.gmail_subscription
            or (not connected and not settings.gmail_email)):
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
        notification_id, email, history_id = parse_notification(payload, settings, connected=connected)
    except ServiceError:
        receive_logger.warning('gmail push_rejected code=INVALID_NOTIFICATION_OR_SUBSCRIPTION')
        raise
    if connected:
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
    service = get_service(request)
    try:
        await run_in_threadpool(service.rpc.call, 'enqueue_gmail_notification', p_subscription=settings.gmail_subscription,
                                p_pubsub_message_id=notification_id, p_email=email, p_history_id=history_id)
    except Exception:
        receive_logger.warning('gmail push_enqueue_failed mode=legacy')
        raise ServiceError('INGEST_UNAVAILABLE', '알림을 영속 접수하지 못했습니다.', 503) from None
    receive_logger.info('gmail push_enqueued mode=legacy')
    return Response(status_code=204)


@router.post('/api/v1/llm/classify-mail', dependencies=protected)
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

