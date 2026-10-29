from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import UUID

from app.core.errors import ServiceError, error_payload
from app.core.idempotency import request_in_progress
from app.core.request_context import current_request_id
from app.features.mailboxes.domain.schemas import GoogleConnection, READ_SCOPE, SEND_SCOPE, GooglePrincipal
from app.features.mailboxes.infrastructure.google import GoogleRejected


@dataclass(frozen=True)
class StoredResponse:
    status: int
    body: dict


def not_connected():
    return GoogleConnection(mailbox_ref=None, email=None, status='NOT_CONNECTED',
        capabilities={'read': False, 'send': False}, watch={'started': False, 'expiration': None})


def watch_result(raw):
    expiration = datetime.fromtimestamp(int(raw['expiration']) / 1000, timezone.utc)
    if expiration <= datetime.now(timezone.utc) or not str(raw['historyId']).isdigit():
        raise ValueError('Invalid watch')
    return {'started': True, 'expiration': expiration.isoformat().replace('+00:00', 'Z')}


class GoogleConnectionService:
    def __init__(self, settings, repository, google, cipher):
        self.settings, self.repo, self.google, self.cipher = settings, repository, google, cipher

    def connection(self, principal):
        row = self.repo.get(principal.user_id)
        if row is None:
            return not_connected()
        if row['google_sub'] != principal.google_sub:
            raise ServiceError('GOOGLE_RECONNECT_REQUIRED', 'Google 계정을 다시 연결해 주세요.', 401)
        result = GoogleConnection.model_validate(row['public'])
        # GET is a read: expiration is computed without renewing or mutating state.
        if result.watch.expiration and result.watch.expiration <= datetime.now(timezone.utc):
            result.watch.started = False
        return result

    def connect(self, principal, payload, key):
        if not key or not key.strip():
            raise ServiceError('INVALID_REQUEST', 'Idempotency-Key가 필요합니다.', 422, fields=['header.Idempotency-Key'])
        if len(key) > 256:
            raise ServiceError('INVALID_REQUEST', 'Idempotency-Key가 너무 깁니다.', 422, fields=['header.Idempotency-Key'])
        access = payload.provider_access_token.get_secret_value()
        refresh = payload.provider_refresh_token.get_secret_value() if payload.provider_refresh_token else None
        claim = self.repo.claim(principal.user_id, key, self.cipher.body_hash(access, refresh))
        kind = claim['kind']
        if kind == 'REPLAY':
            response = claim['response']
            return StoredResponse(response['status'], response['body'])
        if kind == 'IN_PROGRESS':
            raise request_in_progress()
        if kind == 'CONFLICT':
            raise ServiceError('IDEMPOTENCY_KEY_REUSED', '같은 키에 다른 요청 본문을 사용할 수 없습니다.', 409, retryable=False)
        request_id = str(UUID(claim['request_id']))
        try:
            grant = self.google.inspect(access, principal)
        except GoogleRejected:
            result = StoredResponse(401, error_payload('GOOGLE_RECONNECT_REQUIRED', 'Google 계정과 권한을 다시 확인해 주세요.', 401))
            self.repo.finish(request_id, principal.user_id, {'status': result.status, 'body': result.body}, None)
            return result
        except ServiceError:
            self.repo.retryable(request_id, principal.user_id)
            raise
        needs_reconnect = refresh is None
        try:
            if refresh:
                try:
                    grant = self.google.refresh(refresh, principal)
                except GoogleRejected:
                    needs_reconnect = True
            scopes = grant.scopes
            read, send = READ_SCOPE in scopes, SEND_SCOPE in scopes
            # Declining both Gmail scopes is a valid consent outcome, not a technical failure.
            # PDF Auth flow (pages 5/11): both granted => CONNECTED; any missing => PARTIAL.
            status = 'NEEDS_RECONNECT' if needs_reconnect else 'CONNECTED' if read and send else 'PARTIAL'
            watch = {'started': False, 'expiration': None}
            if read and not needs_reconnect and self.settings.gmail_push_configured:
                try:
                    watch = watch_result(self.google.watch(grant))
                except (ServiceError, GoogleRejected, ValueError, KeyError, TypeError, OverflowError):
                    # Watch setup failure falls back to the durable periodic sync job.
                    pass
            # mailbox_ref is allocated by the claim transaction; stable across reconnects.
            mailbox_ref = str(UUID(claim['mailbox_ref']))
            public = GoogleConnection(mailbox_ref=mailbox_ref, email=grant.email, status=status,
                capabilities={'read': read, 'send': send}, watch=watch)
            connection = {
                'mailbox_ref': mailbox_ref, 'google_sub': principal.google_sub, 'email': grant.email,
                'public': public.model_dump(mode='json'), 'scopes': sorted(scopes),
                'encrypted_refresh_token': None if needs_reconnect else self.cipher.encrypt(
                    principal.user_id, principal.google_sub, grant.refresh_token or refresh),
                'sync_requested': read and not needs_reconnect,
            }
            # Capture the entire first envelope, including request_id, for exact replay.
            result = StoredResponse(200, {'data': public.model_dump(mode='json'),
                                          'meta': {'request_id': current_request_id()}})
        except ServiceError:
            self.repo.retryable(request_id, principal.user_id)
            raise
        # Finish atomically persists ciphertext + public state + first response + first-sync job.
        # If acknowledgement is lost, never release the claim or call Google again automatically.
        self.repo.finish(request_id, principal.user_id, {'status': result.status, 'body': result.body}, connection)
        return result

    def authorized(self, user_id, capability):
        """Load permission and refresh from DB every time a read/send node starts an operation."""
        if capability not in {'read', 'send'}:
            raise ValueError('Unknown capability')
        row = self.repo.get(user_id)
        if not row or row['public']['status'] not in {'CONNECTED', 'PARTIAL'}:
            raise ServiceError('GOOGLE_RECONNECT_REQUIRED', 'Gmail을 다시 연결해 주세요.', 401)
        if row['public']['capabilities'].get(capability) is not True:
            raise ServiceError('GMAIL_SCOPE_REQUIRED', '허용된 Gmail 권한이 필요합니다.', 403)
        principal = GooglePrincipal(user_id, row['google_sub'], row['email'])
        refresh = self.cipher.decrypt(user_id, principal.google_sub, row['encrypted_refresh_token'])
        try:
            grant = self.google.refresh(refresh, principal)
        except GoogleRejected:
            self.repo.mark_reconnect(row)
            raise ServiceError('GOOGLE_RECONNECT_REQUIRED', 'Google 권한이 만료되거나 철회됐습니다.', 401) from None
        scope = READ_SCOPE if capability == 'read' else SEND_SCOPE
        if scope not in grant.scopes:
            self.repo.mark_reconnect(row)
            raise ServiceError('GMAIL_SCOPE_REQUIRED', 'Google 권한을 다시 확인해 주세요.', 403)
        if grant.refresh_token and grant.refresh_token != refresh:
            self.repo.rotate_token(row, self.cipher.encrypt(user_id, principal.google_sub, grant.refresh_token))
        return row, grant
