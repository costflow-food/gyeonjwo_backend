"""Connected mailbox worker: durable jobs, no token or message body logging."""
import logging
import threading
import time
from datetime import datetime, timedelta, timezone

from app.core.errors import ServiceError
from app.core.leases import LeaseHeartbeat
from app.features.mail.application.classification import classify_or_raise
from app.features.mailboxes.application.ingestion import determine_link, message_metadata
from app.features.mailboxes.application.service import watch_result
from app.features.mailboxes.infrastructure.google import GoogleRejected
from app.shared.infrastructure.llm import JsonAgent

logger = logging.getLogger('gyeonjwo')


class ClassificationNode:
    """Team rules/prompt shared with the classification API; failures remain failures."""
    def __init__(self, settings):
        self.settings = settings

    def __call__(self, message):
        result = classify_or_raise(JsonAgent(self.settings), message['subject'][:500], message['body_text'])
        return {'status': {'TRUE': 'BUSINESS', 'FALSE': 'NON_BUSINESS', 'PENDING': 'UNCERTAIN'}[result.status],
                'reason': result.reason}


class ConnectedMailboxWorker:
    def __init__(self, service, classifier=None):
        self.s, self.repo = service, service.repo
        self.classifier = classifier or ClassificationNode(service.settings)
        self.stop, self.wake = threading.Event(), threading.Event()
        self.thread, self.running, self.last_poll = None, False, 0

    def _access(self, job):
        row, grant = self.s.authorized(job['user_id'], 'read')
        if row['mailbox_ref'] != job['mailbox_ref'] or row['connection_revision'] != job['connection_revision']:
            raise ServiceError('SYNC_OWNERSHIP_LOST', '메일함 연결 버전이 변경됐습니다.', 409)
        return row, grant

    def process_once(self):
        job = self.repo.claim_sync()
        if job is None:
            return False
        try:
            with LeaseHeartbeat(lambda: self.repo.heartbeat(job)) as lease:
                row, grant = self._access(job)
                watch = row['public']['watch']
                expiration = watch.get('expiration')
                expiring = not expiration or datetime.fromisoformat(expiration.replace('Z', '+00:00')) < datetime.now(timezone.utc) + timedelta(days=1)
                if expiring and self.s.settings.gmail_push_configured:
                    try:
                        watch = watch_result(self.s.google.watch(grant))
                    except (ServiceError, GoogleRejected, ValueError, KeyError, TypeError, OverflowError):
                        watch = {'started': False, 'expiration': None}
                try:
                    ids, latest = self.s.google.message_ids(grant, job.get('last_history_id'))
                except ServiceError as error:
                    if error.code != 'GMAIL_NOT_FOUND':
                        raise
                    ids, latest = self.s.google.message_ids(grant, None)
                for message_id in ids:
                    lease.check()
                    if self.repo.message_exists(job['mailbox_ref'], message_id):
                        continue
                    _, grant = self._access(job)
                    try:
                        message = message_metadata(self.s.google.message(grant, message_id))
                    except ServiceError as error:
                        if error.code != 'GMAIL_NOT_FOUND':
                            raise
                        message = {'gmail_message_id': message_id, 'deleted': True}
                        self.repo.ingest(job, message, None, {'kind': 'DELETED', 'case_id': None})
                        continue
                    # Business reply evidence takes precedence over classification of short replies.
                    link = determine_link(message, self.repo.reply_candidates(job, message))
                    classification = None if link['kind'] == 'EXISTING_CASE' else self.classifier(message)
                    if classification is not None and classification.get('status') not in {'BUSINESS', 'NON_BUSINESS', 'UNCERTAIN'}:
                        raise ServiceError('CLASSIFICATION_UNAVAILABLE', '분류 결과를 확인할 수 없습니다.', 503)
                    lease.check()
                    self.repo.ingest(job, message, classification, link)
                lease.check()
                if self.repo.finish_sync(job, latest, watch) is not True:
                    raise ServiceError('SYNC_OWNERSHIP_LOST', '동기화 커서 저장을 확인할 수 없습니다.', 409)
            return True
        except Exception:
            # Failed/uncertain and no-new-mail are different. Cursor never advances here.
            self.repo.fail_sync(job, 'GMAIL_SYNC_FAILED')
            logger.warning('connected mailbox sync failed code=GMAIL_SYNC_FAILED')
            return True

    def start(self):
        def loop():
            self.running = True
            try:
                while not self.stop.is_set():
                    try:
                        now = time.monotonic()
                        # DB owns the per-mailbox 90s due time. A second 90s gate here
                        # could miss a just-not-due job and delay it another full interval.
                        if not self.last_poll or now - self.last_poll >= 5:
                            self.repo.schedule_poll()
                            self.last_poll = now
                        if self.process_once():
                            continue
                    except Exception:
                        logger.warning('connected mailbox worker unavailable')
                    self.wake.wait(5)
                    self.wake.clear()
            finally:
                self.running = False
        self.thread = threading.Thread(target=loop, daemon=True, name='connected-gmail-worker')
        self.thread.start()

    def close(self):
        self.stop.set()
        self.wake.set()
        if self.thread:
            self.thread.join(timeout=25)
