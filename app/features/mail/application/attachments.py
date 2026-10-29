"""Shared, read-only Gmail attachment retrieval for HTTP and extraction nodes.

No disk writes, persistence, filename logging, or provider-body error disclosure.
The stored MIME part reference is authoritative; stale Gmail attachment IDs are not.
"""
import base64
import binascii
import re
from dataclasses import dataclass
from urllib.parse import quote

from app.core.errors import ServiceError
from app.features.mailboxes.infrastructure.google import GoogleRejected


def missing():
    return ServiceError('RESOURCE_NOT_FOUND', '메일 또는 첨부를 찾을 수 없습니다.', 404)


@dataclass(frozen=True, repr=False)
class AttachmentContent:
    content: bytes
    filename: str
    mime_type: str

    def headers(self):
        disposition = 'inline' if self.mime_type.split(';', 1)[0].lower() == 'application/pdf' or self.mime_type.lower().startswith('image/') else 'attachment'
        # Strip path/control characters before RFC 5987 encoding. Never interpolate
        # an unescaped provider filename into an HTTP header.
        name = re.sub(r'[\x00-\x1f\x7f]', '', self.filename.replace('\\', '/').rsplit('/', 1)[-1]) or 'attachment'
        return {'Content-Type': self.mime_type,
                'Content-Disposition': disposition + "; filename=\"attachment\"; filename*=UTF-8''" + quote(name, safe=''),
                'Cache-Control': 'private, no-store', 'X-Content-Type-Options': 'nosniff',
                'Content-Security-Policy': "sandbox; default-src 'none'"}


def fetch_gmail_attachment(repo, mailbox_service, owner_id, mail_id, attachment_id):
    """Return bytes in memory after owner, mailbox and current read-scope checks.

    Public attachment identifiers should be the stable provider_part_ref (part_id
    for legacy ingestion), or an explicit persisted attachment_id. The original
    provider attachment reference is accepted only as a stored legacy identifier.
    """
    message = repo.message(owner_id, mail_id)
    attachments = message.get('attachments_jsonb')
    if not isinstance(attachments, list):
        raise missing()
    matches = [a for a in attachments if isinstance(a, dict) and attachment_id in {
        a.get('attachment_id'), a.get('provider_part_ref'), a.get('part_id'),
        a.get('provider_attachment_ref'), a.get('gmail_attachment_id')}]
    if len(matches) != 1:
        raise missing()
    stored = matches[0]
    part_id = stored.get('provider_part_ref', stored.get('part_id'))
    if not isinstance(part_id, str) or str(message.get('provider', '')).upper() != 'GMAIL':
        raise missing()
    try:
        connection, grant = mailbox_service.authorized(owner_id, 'read')
        if connection['mailbox_ref'] != message['mailbox_ref']:
            raise ServiceError('GOOGLE_RECONNECT_REQUIRED', '메일함을 다시 연결해 주세요.', 401)
        raw = mailbox_service.google.message(grant, message['provider_message_id'])
        stack, found = [raw['payload']], []
        while stack:
            part = stack.pop()
            if part.get('partId') == part_id:
                found.append(part)
            stack.extend(part.get('parts', []))
        if len(found) != 1:
            raise ServiceError('SOURCE_READ_FAILED', 'Gmail 원본 첨부를 확인할 수 없습니다.', 502)
        part = found[0]
        body = part.get('body') or {}
        if body.get('attachmentId'):
            body = mailbox_service.google.attachment(grant, message['provider_message_id'], body['attachmentId'])
        encoded = body.get('data')
        if not isinstance(encoded, str):
            raise ValueError('Missing attachment data')
        content = base64.b64decode(encoded + '=' * (-len(encoded) % 4), altchars=b'-_', validate=True)
        if type(body.get('size')) is not int or body['size'] != len(content):
            raise ValueError('Invalid attachment size')
        mime = part.get('mimeType') or 'application/octet-stream'
        if not isinstance(mime, str) or not re.fullmatch(r'[A-Za-z0-9!#$&^_.+-]+/[A-Za-z0-9!#$&^_.+-]+', mime):
            raise ValueError('Invalid content type')
        filename = part.get('filename') or stored.get('filename') or 'attachment'
        if not isinstance(filename, str):
            raise ValueError('Invalid filename')
        return AttachmentContent(content, filename, mime)
    except GoogleRejected:
        raise ServiceError('MAILBOX_RECONNECT_REQUIRED', 'Google 메일함을 다시 연결해 주세요.', 409,
                           retryable=False, next_action='RECONNECT_MAILBOX') from None
    except ServiceError as error:
        if error.code in {'GOOGLE_RECONNECT_REQUIRED', 'GMAIL_SCOPE_REQUIRED', 'CREDENTIALS_UNAVAILABLE'}:
            raise ServiceError('MAILBOX_RECONNECT_REQUIRED', 'Google 메일함을 다시 연결해 주세요.', 409,
                               retryable=False, next_action='RECONNECT_MAILBOX') from None
        if error.status == 503 and error.code not in {'GOOGLE_UNAVAILABLE'}:
            raise
        raise ServiceError('SOURCE_READ_FAILED', 'Gmail 원본 첨부를 읽을 수 없습니다.', 502) from None
    except (KeyError, TypeError, ValueError, AttributeError, binascii.Error):
        raise ServiceError('SOURCE_READ_FAILED', 'Gmail 원본 첨부를 읽을 수 없습니다.', 502) from None
