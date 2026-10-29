"""Supabase RPC boundary. SQL is owned/deployed by the DB teammate.

See docs/GOOGLE_MAILBOX_DB_CONTRACT.md; never substitute process memory for these
transactions. Raw provider tokens and request bodies are never RPC parameters.
"""
from app.core.errors import ServiceError


class MailboxRepository:
    def __init__(self, client):
        self.client = client

    def call(self, name, **params):
        try:
            return self.client.rpc(name, params).execute().data
        except Exception as error:
            if getattr(error,'code',None) == 'PGRST202':
                raise ServiceError('MAILBOX_SCHEMA_REQUIRED','계정 연결용 DB 함수를 적용해야 합니다.',503,
                                   retryable=False,next_action='CONTACT_SERVER_ADMIN') from None
            raise ServiceError('MAILBOX_STORAGE_UNAVAILABLE', '메일함 저장소를 확인할 수 없습니다.', 503) from None

    def claim(self, user_id, key, body_hash):
        result = self.call('claim_google_connection_request', p_user_id=user_id, p_key=key, p_body_hash=body_hash)
        if not isinstance(result, dict) or result.get('kind') not in {'ACQUIRED', 'REPLAY', 'IN_PROGRESS', 'CONFLICT'}:
            raise ServiceError('MAILBOX_STORAGE_UNAVAILABLE', '요청 접수 결과를 확인할 수 없습니다.', 503)
        return result

    def finish(self, request_id, user_id, response, connection):
        result = self.call('finish_google_connection_request', p_request_id=request_id, p_user_id=user_id,
                           p_response=response, p_connection=connection)
        if result is not True:
            raise ServiceError('MAILBOX_STORAGE_UNAVAILABLE', '연결 결과 저장을 확인할 수 없습니다.', 503)

    def retryable(self, request_id, user_id):
        # Called only before final transaction begins. A lost finish acknowledgement must stay pending.
        self.call('retry_google_connection_request', p_request_id=request_id, p_user_id=user_id)

    def get(self, user_id):
        result = self.call('get_google_mailbox_connection', p_user_id=user_id)
        if result is not None and (not isinstance(result, dict) or result.get('user_id') != user_id):
            raise ServiceError('MAILBOX_STORAGE_UNAVAILABLE', '메일함 소유 범위를 확인할 수 없습니다.', 503)
        return result

    def mark_reconnect(self, row):
        self.call('mark_google_mailbox_reconnect', p_mailbox_ref=row['mailbox_ref'],
                  p_connection_revision=row['connection_revision'])

    def rotate_token(self, row, ciphertext):
        result = self.call('rotate_connected_google_token', p_mailbox_ref=row['mailbox_ref'],
                          p_connection_revision=row['connection_revision'],
                          p_previous_ciphertext=row['encrypted_refresh_token'], p_ciphertext=ciphertext)
        if result is not True:
            raise ServiceError('GOOGLE_RECONNECT_REQUIRED', '연결 정보가 변경됐습니다. 다시 확인해 주세요.', 401)

    def enqueue_push(self, subscription, message_id, email, history_id):
        accepted = self.call('enqueue_connected_gmail_notification', p_subscription=subscription,
                             p_message_id=message_id, p_email=email, p_history_id=history_id)
        if accepted is not True:
            raise ServiceError('INGEST_UNAVAILABLE', '알림을 저장하지 못했습니다.', 503)

    def schedule_poll(self):
        self.call('schedule_connected_gmail_sync')

    def claim_sync(self):
        return self.call('claim_connected_gmail_sync')

    def heartbeat(self, job):
        result = self.call('heartbeat_connected_gmail_sync', p_job_id=job['job_id'], p_claim_token=job['claim_token'])
        if result is not True:
            raise ServiceError('SYNC_OWNERSHIP_LOST', '수집 작업의 소유권이 변경됐습니다.', 409)

    def finish_sync(self, job, history_id, watch):
        return self.call('finish_connected_gmail_sync', p_job_id=job['job_id'], p_claim_token=job['claim_token'],
                         p_connection_revision=job['connection_revision'], p_history_id=history_id, p_watch=watch)

    def fail_sync(self, job, code):
        self.call('fail_connected_gmail_sync', p_job_id=job['job_id'], p_claim_token=job['claim_token'], p_code=code)

    def message_exists(self, mailbox_ref, message_id):
        return self.call('connected_gmail_message_exists', p_mailbox_ref=mailbox_ref, p_message_id=message_id) is True

    def reply_candidates(self, job, message):
        rows = self.call('find_connected_gmail_reply_candidates', p_mailbox_ref=job['mailbox_ref'], p_message=message)
        if not isinstance(rows, list):
            raise ServiceError('INGEST_UNAVAILABLE', '회신 근거를 확인할 수 없습니다.', 503)
        return rows

    def ingest(self, job, message, classification, link):
        result = self.call('ingest_connected_gmail_message', p_job_id=job['job_id'], p_claim_token=job['claim_token'],
                           p_connection_revision=job['connection_revision'], p_message=message,
                           p_classification=classification, p_link=link)
        if not isinstance(result, dict) or result.get('outcome') not in {
            'NEW_CASE', 'LINKED_REPLY', 'ARCHIVED_REPLY', 'INTAKE', 'NON_BUSINESS', 'DUPLICATE', 'DELETED'}:
            raise ServiceError('INGEST_UNAVAILABLE', '메일 저장 결과를 확인할 수 없습니다.', 503)
        return result
