import base64
import json
import threading
from datetime import datetime, timezone
from urllib.parse import quote

import httpx
from google.auth.transport.requests import Request as GoogleRequest
from google.oauth2 import id_token
from google.oauth2.credentials import Credentials

from app.core.errors import ServiceError


def decode_urlsafe(value):
    return base64.b64decode(value + '=' * (-len(value) % 4), altchars=b'-_', validate=True)


class BoundedGoogleRequest(GoogleRequest):
    def __call__(self, *args, **kwargs):
        kwargs['timeout'] = 15
        return super().__call__(*args, **kwargs)


def verify_push(token, settings):
    try:
        claims = id_token.verify_oauth2_token(token, BoundedGoogleRequest(), settings.pubsub_audience)
        if claims.get('email') != settings.pubsub_service_account or claims.get('email_verified') is not True:
            raise ValueError('Unexpected service account')
    except Exception:
        raise ServiceError('UNAUTHORIZED_PUSH', 'Pub/Sub 인증에 실패했습니다.', 401) from None


def parse_notification(body, settings, *, connected=False):
    try:
        if body.get('subscription') != settings.gmail_subscription:
            raise ServiceError('UNEXPECTED_SUBSCRIPTION', '허용하지 않은 구독입니다.', 403)
        message = body['message']
        if not isinstance(message['messageId'], str) or not message['messageId'].strip():
            raise ValueError('Missing message ID')
        decoded = json.loads(decode_urlsafe(message['data']))
        email = decoded['emailAddress'].lower()
        history = decoded['historyId']
        if not isinstance(history, str) or not history.isascii() or not history.isdigit() or len(history) > 30:
            raise ValueError('Invalid history ID')
        if not isinstance(email, str) or '@' not in email:
            raise ValueError('Invalid mailbox')
        if not connected and email != settings.gmail_email.lower():
            raise ServiceError('UNEXPECTED_SUBSCRIPTION', '허용하지 않은 메일함입니다.', 403)
        return message['messageId'], email, history
    except (KeyError, ValueError, TypeError, AttributeError):
        raise ServiceError('INVALID_NOTIFICATION', '잘못된 Gmail 알림 형식입니다.', 400) from None


class GmailClient:
    def __init__(self, settings, transport=None):
        self.settings, self.transport = settings, transport
        self.lock = threading.Lock()
        self.credentials = None
        self.mailbox_verified = False

    def request(self, path, *, method='GET', params=None, body=None):
        self.settings.require_gmail()
        with self.lock:
            if self.credentials is None:
                self.credentials = Credentials(token=None, refresh_token=self.settings.gmail_refresh_token.get_secret_value(),
                                               token_uri='https://oauth2.googleapis.com/token',
                                               client_id=self.settings.gmail_client_id,
                                               client_secret=self.settings.gmail_client_secret.get_secret_value())
            if not self.credentials.valid:
                try:
                    self.credentials.refresh(BoundedGoogleRequest())
                except Exception:
                    raise ServiceError('GMAIL_AUTH_FAILED', 'Gmail 인증 갱신에 실패했습니다.', 502) from None
            token = self.credentials.token
        try:
            with httpx.Client(timeout=30, transport=self.transport) as client:
                response = client.request(method, 'https://gmail.googleapis.com/gmail/v1/users/me/' + path,
                                          headers={'Authorization': 'Bearer ' + token}, params=params, json=body)
            if response.status_code == 404:
                raise ServiceError('GMAIL_NOT_FOUND', 'Gmail 항목 또는 동기화 커서를 찾을 수 없습니다.', 404)
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPError, ValueError):
            raise ServiceError('GMAIL_UNAVAILABLE', 'Gmail 조회에 실패했습니다.', 502) from None

    def profile(self):
        profile = self.request('profile')
        if str(profile.get('emailAddress', '')).lower() != self.settings.gmail_email.lower():
            raise ServiceError('GMAIL_MAILBOX_MISMATCH', 'OAuth 계정과 설정된 메일함이 다릅니다.', 503)
        self.mailbox_verified = True
        return profile

    def watch(self):
        self.profile()
        return self.request('watch', method='POST', body={'topicName': self.settings.gmail_topic,
                                                         'labelIds': ['INBOX'], 'labelFilterBehavior': 'include'})

    def changed_ids(self, history_id):
        if not self.mailbox_verified:
            self.profile()
        ids, token, latest = set(), None, str(history_id)
        while True:
            params = {'startHistoryId': str(history_id), 'historyTypes': 'messageAdded', 'maxResults': 500}
            if token:
                params['pageToken'] = token
            page = self.request('history', params=params)
            for entry in page.get('history', []):
                for added in entry.get('messagesAdded', []):
                    message = added['message']
                    if 'INBOX' in message.get('labelIds', []):
                        ids.add(message['id'])
            latest = str(max(int(latest), int(page['historyId'])))
            token = page.get('nextPageToken')
            if not token:
                return sorted(ids), latest

    def full_ids(self):
        # Cursor captured BEFORE listing: later changes are replayed, never skipped.
        profile = self.profile()
        ids, token = [], None
        while True:
            params = {'labelIds': 'INBOX', 'maxResults': 500}
            if token:
                params['pageToken'] = token
            page = self.request('messages', params=params)
            ids.extend(message['id'] for message in page.get('messages', []))
            token = page.get('nextPageToken')
            if not token:
                return sorted(set(ids)), str(profile['historyId'])

    def raw_mail(self, message_id):
        if not self.mailbox_verified:
            self.profile()
        response = self.request('messages/' + quote(message_id, safe=''), params={'format': 'raw'})
        return decode_urlsafe(response['raw']), datetime.fromtimestamp(int(response['internalDate']) / 1000, timezone.utc)
