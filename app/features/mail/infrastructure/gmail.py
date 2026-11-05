import base64
import json

from google.auth.transport.requests import Request as GoogleRequest
from google.oauth2 import id_token

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


def parse_notification(body, settings):
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
        return message['messageId'], email, history
    except (KeyError, ValueError, TypeError, AttributeError):
        raise ServiceError('INVALID_NOTIFICATION', '잘못된 Gmail 알림 형식입니다.', 400) from None
