"""Bounded Google REST adapter. No URL/body logging (tokeninfo has a secret query).

http.client is deliberately used instead of HTTP clients that log tokeninfo URLs
at INFO. TLS certificate verification remains enabled and redirects are not followed.
"""
import http.client
import json
import logging
import ssl
import time
import base64
from email.message import EmailMessage
from email.policy import SMTP
from email.utils import format_datetime
from datetime import datetime, timezone
from uuid import uuid4
from dataclasses import dataclass
from urllib.parse import quote, urlencode

from app.core.errors import ServiceError
from app.features.mailboxes.domain.schemas import READ_SCOPE, SEND_SCOPE

logger = logging.getLogger('uvicorn.error.gmail_receive')
CONTROL_RESPONSE_LIMIT = 4 * 1024 * 1024
# 메일 원문·첨부 응답 상한. 저장 한도(storage.MAX_BYTES, 50MB)를 base64로 담은 크기(약 4/3배)에 JSON 여유를 더했다
CONTENT_RESPONSE_LIMIT = 72 * 1024 * 1024


def operation_name(host, path):
    """Only fixed labels may be logged: paths/queries can contain credentials."""
    route = path.split('?', 1)[0]
    if host == 'oauth2.googleapis.com':
        return 'token_refresh' if route == '/token' else 'token_inspect'
    if host == 'openidconnect.googleapis.com':
        return 'identity_read'
    if host == 'gmail.googleapis.com':
        if '/attachments/' in route:
            return 'attachment_read'
        return {'watch': 'watch_register', 'history': 'history_read',
                'messages': 'message_list', 'profile': 'profile_read'}.get(
                    route.rsplit('/', 1)[-1], 'message_read')
    return 'google_request'


class GoogleRejected(Exception):
    """Definitive OAuth rejection. Never retains Google's raw error response."""


@dataclass(frozen=True, repr=False)
class Grant:
    access_token: str
    google_sub: str
    email: str
    scopes: frozenset[str]
    refresh_token: str | None = None


def https_json(method, host, path, headers, body):
    conn = http.client.HTTPSConnection(host, timeout=20, context=ssl.create_default_context())
    conn.set_debuglevel(0)
    try:
        conn.request(method, path, body=body, headers=headers)
        response = conn.getresponse()
        # Full messages can embed attachment data; attachment JSON is base64 encoded.
        # Content endpoints get a larger cap than control responses, but are still bounded.
        content = host == 'gmail.googleapis.com' and '/messages/' in path
        limit = CONTENT_RESPONSE_LIMIT if content else CONTROL_RESPONSE_LIMIT
        raw = response.read(limit + 1)
        if len(raw) > limit:
            raise ValueError('Response limit exceeded')
        return response.status, json.loads(raw)
    finally:
        conn.close()


class GoogleAPI:
    def __init__(self, settings, transport=https_json):
        self.settings, self.transport = settings, transport

    def _call(self, method, host, path, *, token=None, form=None, payload=None, oauth=False):
        operation = operation_name(host, path)
        operation_id = uuid4().hex
        started = time.monotonic()
        logger.info('gmail provider_started operation=%s operation_id=%s', operation, operation_id)
        headers, body = {'Accept': 'application/json'}, None
        if token:
            headers['Authorization'] = 'Bearer ' + token
        if form is not None:
            headers['Content-Type'] = 'application/x-www-form-urlencoded'
            body = urlencode(form).encode()
        elif payload is not None:
            headers['Content-Type'] = 'application/json'
            body = json.dumps(payload).encode()
        try:
            status, data = self.transport(method, host, path, headers, body)
        except Exception:
            logger.warning('gmail provider_failed operation=%s operation_id=%s code=TRANSPORT_OR_RESPONSE_ERROR',
                           operation, operation_id)
            raise ServiceError('GOOGLE_UNAVAILABLE', 'Google 서비스에 연결할 수 없습니다.', 503) from None
        if not isinstance(status, int) or isinstance(status, bool):
            logger.warning('gmail provider_failed operation=%s operation_id=%s code=INVALID_RESPONSE', operation, operation_id)
            raise ServiceError('GOOGLE_UNAVAILABLE', 'Google 응답을 확인할 수 없습니다.', 503)
        if not 200 <= status < 300 or not isinstance(data, dict):
            logger.warning('gmail provider_failed operation=%s operation_id=%s http_status=%d',
                           operation, operation_id, status)
        if status == 429 or status >= 500:
            raise ServiceError('GOOGLE_UNAVAILABLE', 'Google 요청을 잠시 후 다시 시도해 주세요.', 503)
        if oauth and status in (400, 401, 403):
            raise GoogleRejected() from None
        if status == 404:
            raise ServiceError('GMAIL_NOT_FOUND', '메일 또는 동기화 커서를 찾을 수 없습니다.', 404)
        if status in (401, 403):
            raise GoogleRejected() from None
        if not 200 <= status < 300 or not isinstance(data, dict):
            raise ServiceError('GOOGLE_UNAVAILABLE', 'Google 응답을 확인할 수 없습니다.', 503)
        logger.info('gmail provider_completed operation=%s operation_id=%s http_status=%d elapsed_ms=%d',
                    operation, operation_id, status, int((time.monotonic() - started) * 1000))
        return data

    def inspect(self, access_token, principal):
        data = self._call('GET', 'oauth2.googleapis.com', '/tokeninfo?' + urlencode({'access_token': access_token}), oauth=True)
        if data.get('aud') != self.settings.gmail_client_id:
            raise GoogleRejected()
        try:
            if int(data.get('expires_in', 0)) <= 0:
                raise GoogleRejected()
        except (TypeError, ValueError):
            raise GoogleRejected() from None
        # Stable Google subject is authoritative; email is checked as an additional binding.
        subject, email = data.get('sub') or data.get('user_id'), data.get('email')
        verified = data.get('email_verified')
        if not subject or not email or verified not in (True, 'true'):
            user = self._call('GET', 'openidconnect.googleapis.com', '/v1/userinfo', token=access_token, oauth=True)
            subject, email, verified = user.get('sub'), user.get('email'), user.get('email_verified')
        if (subject != principal.google_sub or not isinstance(email, str)
                or email.casefold() != principal.email.casefold() or verified not in (True, 'true')):
            raise GoogleRejected()
        scope = data.get('scope')
        if not isinstance(scope, str):
            raise GoogleRejected()
        return Grant(access_token, subject, email.casefold(), frozenset(scope.split()))

    def refresh(self, refresh_token, principal):
        data = self._call('POST', 'oauth2.googleapis.com', '/token', form={
            'grant_type': 'refresh_token', 'client_id': self.settings.gmail_client_id,
            'client_secret': self.settings.gmail_client_secret.get_secret_value(), 'refresh_token': refresh_token}, oauth=True)
        access = data.get('access_token')
        if not isinstance(access, str) or not access or str(data.get('token_type', '')).casefold() != 'bearer':
            raise GoogleRejected()
        grant = self.inspect(access, principal)
        rotated = data.get('refresh_token') or refresh_token
        if not isinstance(rotated, str):
            raise GoogleRejected()
        return Grant(grant.access_token, grant.google_sub, grant.email, grant.scopes, rotated)

    def gmail(self, grant, path, *, method='GET', payload=None):
        # Called on every request, including watch renewal and worker reads.
        sending = path.split('?', 1)[0].rstrip('/') == 'messages/send'
        required = SEND_SCOPE if sending else READ_SCOPE
        if required not in grant.scopes:
            logger.warning('gmail operation_blocked operation=%s code=GMAIL_SCOPE_REQUIRED',
                           'send' if sending else 'read')
            raise ServiceError('GMAIL_SCOPE_REQUIRED', '허용된 Gmail 권한이 필요합니다.', 403, retryable=False)
        if sending or (method != 'GET' and not (method == 'POST' and path == 'watch')):
            logger.warning('gmail operation_blocked operation=%s code=APPROVED_SEND_NODE_REQUIRED',
                           'send' if sending else 'write')
            # An OAuth connection is not approval to send; only send_claimed()
            # accepts the internal node's validated DB authorization snapshot.
            raise ServiceError('ACTION_NOT_ALLOWED', '메일은 승인 설정을 검사하는 발송 노드를 통해 보내야 합니다.', 409)
        return self._call(method, 'gmail.googleapis.com', '/gmail/v1/users/me/' + path,
                          token=grant.access_token, payload=payload)

    def watch(self, grant):
        return self.gmail(grant, 'watch', method='POST', payload={
            'topicName': self.settings.gmail_topic, 'labelIds': ['INBOX'], 'labelFilterBehavior': 'include'})

    def profile(self, grant):
        profile = self.gmail(grant, 'profile')
        if str(profile.get('emailAddress', '')).casefold() != grant.email:
            raise GoogleRejected()
        return profile

    def message(self, grant, message_id):
        return self.gmail(grant, 'messages/' + quote(message_id, safe='') + '?format=full')

    def message_headers(self, grant, message_id):
        query = urlencode([('format', 'metadata')] + [('metadataHeaders', name)
                           for name in ('Message-ID', 'From', 'To', 'Cc', 'Subject', 'X-Gyeonjwo-Dispatch-ID')])
        return self.gmail(grant, 'messages/' + quote(message_id, safe='') + '?' + query)

    def send_claimed(self, grant, claim):
        """Internal node only: accept a validated DB-authorized content snapshot."""
        from app.features.work_cases.domain.rfq import AuthorizedMail
        if not isinstance(claim, AuthorizedMail) or SEND_SCOPE not in grant.scopes:
            raise ServiceError('ACTION_NOT_ALLOWED', '검증된 발송 접수가 필요합니다.', 409)
        message = EmailMessage(policy=SMTP)
        message['From'], message['To'] = grant.email, claim.recipient
        message['Subject'], message['Message-ID'] = claim.subject, claim.rfc_message_id
        message['X-Gyeonjwo-Dispatch-ID'] = claim.draft_id
        message['Date'] = format_datetime(datetime.now(timezone.utc))
        message.set_content(claim.body_text)
        raw = base64.urlsafe_b64encode(message.as_bytes()).decode().rstrip('=')
        # Deliberately bypass gmail()'s public read-only facade only here. The
        # caller has claimed SENDING; transport is one-shot with no POST retry.
        return self._call('POST', 'gmail.googleapis.com', '/gmail/v1/users/me/messages/send',
                          token=grant.access_token, payload={'raw': raw})

    def recent_sent_ids(self, grant):
        page = self.gmail(grant, 'messages?' + urlencode({'labelIds': 'SENT', 'maxResults': 100}))
        return [row['id'] for row in page.get('messages', [])]

    def sent_by_message_id(self, grant, rfc_message_id):
        data = self.gmail(grant, 'messages?' + urlencode({'q': 'in:sent rfc822msgid:' + rfc_message_id,
                                                        'maxResults': 10}))
        if data.get('nextPageToken'):
            raise ServiceError('SEND_RESULT_UNKNOWN', '발송 결과를 확정할 수 없습니다.', 409)
        return [row['id'] for row in data.get('messages', [])]

    def attachment(self, grant, message_id, attachment_id):
        return self.gmail(grant, 'messages/' + quote(message_id, safe='') + '/attachments/' + quote(attachment_id, safe=''))

    def message_ids(self, grant, history_id, *, initial_limit=None):
        """Bound initial inbox import; never truncate incremental history pages.

        Sample the history cursor BEFORE listing so arrivals during import remain
        eligible for the next incremental pass. Do not follow initial nextPageToken:
        maxResults alone is only a page size and otherwise imports the whole inbox.
        Expired cursors use the separate paginated recovery path.
        """
        ids, token, seen_tokens = set(), None, set()
        if history_id is None:
            limit = self.settings.gmail_initial_sync_limit if initial_limit is None else initial_limit
            if type(limit) is not int or not 1 <= limit <= 500:
                raise ValueError('Initial sync limit must be an integer between 1 and 500')
            latest = str(self.profile(grant)['historyId'])
            logger.info('gmail initial_sync_started limit=%d', limit)
            page = self.gmail(grant, 'messages?' + urlencode({'labelIds': 'INBOX', 'maxResults': limit}))
            # Gmail's first list page is its most recent messages; preserve that
            # order, rather than lexicographically sorting opaque provider IDs.
            recent = list(dict.fromkeys(row['id'] for row in page.get('messages', [])))[:limit]
            logger.info('gmail initial_sync_selected count=%d limit=%d older_pages_skipped=%s',
                        len(recent), limit, bool(page.get('nextPageToken')))
            return recent, latest
        latest = str(history_id)
        while True:
            path, params = 'history', {'startHistoryId': str(history_id), 'historyTypes': 'messageAdded', 'maxResults': 100}
            if token:
                params['pageToken'] = token
            page = self.gmail(grant, path + '?' + urlencode(params))
            for entry in page.get('history', []):
                ids.update(row['message']['id'] for row in entry.get('messagesAdded', [])
                           if 'INBOX' in row['message'].get('labelIds', []))
            latest = str(max(int(latest), int(page['historyId'])))
            token = page.get('nextPageToken')
            if not token:
                return sorted(ids), latest
            if token in seen_tokens:
                raise ServiceError('GOOGLE_UNAVAILABLE', '메일 목록의 페이지를 확인할 수 없습니다.', 503)
            seen_tokens.add(token)

    def recover_message_ids(self, grant, last_synced_at):
        latest = str(self.profile(grant)['historyId'])
        params = {'labelIds': 'INBOX', 'maxResults': 100}
        if last_synced_at is not None:
            timestamp = datetime.fromisoformat(last_synced_at.replace('Z', '+00:00'))
            if timestamp.tzinfo is None:
                raise ValueError('Recovery timestamp must include a timezone')
            # A sync finishes after sampling its cursor. Re-read an overlap
            # around that finish time and rely on durable message deduplication.
            params['q'] = f'after:{int(timestamp.timestamp()) - 86400}'
        ids, seen_tokens = {}, set()
        while True:
            page = self.gmail(grant, 'messages?' + urlencode(params))
            ids.update(dict.fromkeys(row['id'] for row in page.get('messages', [])))
            token = page.get('nextPageToken')
            if not token:
                return list(ids), latest
            if token in seen_tokens:
                raise ServiceError('GOOGLE_UNAVAILABLE', '메일 복구 페이지를 확인할 수 없습니다.', 503)
            seen_tokens.add(token)
            params['pageToken'] = token
