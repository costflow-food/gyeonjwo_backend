"""One process only. Persistent notification queue; results never written to disk."""
import logging
import threading
import time
from datetime import datetime, timezone

from app.features.mail.application.classification import classify
from app.features.review.infrastructure.documents import parse_mail, safe_attachment_name
from app.core.errors import ServiceError
from app.core.leases import LeaseHeartbeat

logger = logging.getLogger('uvicorn.error.gmail_receive')


class GmailWorker:
    def __init__(self, services):
        self.s = services
        self.stop = threading.Event()
        self.thread = None
        self.running = False
        self.last_watch = self.last_sync = 0
        self.last_recovery = 0

    def ingest(self, claim, message_id):
        rpc = self.s.rpc
        if rpc.call('find_work_item', p_mailbox_id=claim['mailbox_id'], p_gmail_message_id=message_id):
            return
        raw, received_at = self.s.gmail.raw_mail(message_id)
        try:
            mail = parse_mail(raw)
        except ValueError:
            # Explicitly excluded malformed mail; do not fabricate missing title/body.
            logger.warning('gmail message_skipped mode=legacy code=INVALID_MAIL')
            return
        work_id = rpc.call('issue_work_id')
        attachments = []
        attempted_paths = [work_id + '/mail.eml']
        try:
            raw_meta = self.s.storage.upload_mail(work_id, raw)
            for index, attachment in enumerate(mail['attachments'], 1):
                name = safe_attachment_name(index, attachment['content_type'])
                attempted_paths.append(work_id + '/' + name)
                attachments.append(self.s.storage.upload_attachment(work_id, name, attachment['original_name'],
                                                                    attachment['content_type'], attachment['content']))
        except Exception:
            # Registration has not been attempted, so this freshly issued prefix is not linked to a work item.
            try:
                self.s.storage.bucket.remove(attempted_paths)
            except Exception:
                logger.warning('unregistered source cleanup failed work_id=%s', work_id)
            raise
        # If this RPC response is lost, preserve uploads: the transaction may already have committed.
        registered = rpc.call('register_work_item', p_work_id=work_id, p_mailbox_id=claim['mailbox_id'],
                              p_gmail_message_id=message_id, p_received_at=received_at, p_sender=mail['sender'],
                              p_subject=mail['subject'], p_mail_body=mail['body'], p_attachments=attachments,
                              p_raw_size_bytes=raw_meta['raw_size_bytes'], p_raw_sha256=raw_meta['raw_sha256'])
        if not registered['created']:
            # Only this newly issued, unregistered prefix is eligible for cleanup.
            if registered['work_id'] != work_id:
                paths = [work_id + '/mail.eml'] + [row['storage_path'] for row in attachments]
                self.s.storage.bucket.remove(paths)
            return
        logger.info('gmail classification_started mode=legacy')
        result = classify(self.s.agent, mail['subject'], mail['body'])
        rpc.call('finish_classification', p_work_id=work_id, p_classification_token=registered['classification_token'],
                 p_status=result.status, p_reason=result.reason)
        logger.info('gmail message_saved mode=legacy classification_completed=true')

    def process_once(self):
        claim = self.s.rpc.call('claim_gmail_notification')
        if not claim:
            return False
        logger.info('gmail sync_started mode=legacy')
        params = {'p_notification_id': claim['notification_id'], 'p_claim_token': claim['claim_token']}
        try:
            with LeaseHeartbeat(lambda: self.s.rpc.call('heartbeat_gmail_notification', **params)) as lease:
                try:
                    if claim['last_history_id'] is None:
                        ids, latest = self.s.gmail.full_ids()
                    else:
                        ids, latest = self.s.gmail.changed_ids(claim['last_history_id'])
                except ServiceError as error:
                    if error.code != 'GMAIL_NOT_FOUND':
                        raise
                    ids, latest = self.s.gmail.full_ids()
                logger.info('gmail messages_found mode=legacy count=%d', len(ids))
                for message_id in ids:
                    lease.check()
                    try:
                        self.ingest(claim, message_id)
                    except ServiceError as error:
                        if error.code != 'GMAIL_NOT_FOUND':
                            raise
                        logger.info('ingest excluded code=GMAIL_DELETED')
                lease.check()
                self.s.rpc.call('finish_gmail_notification', **params, p_last_history_id=latest)
            logger.info('gmail sync_completed mode=legacy')
            return True
        except Exception:
            logger.warning('gmail sync_failed mode=legacy code=GMAIL_SYNC_FAILED')
            # No exception/body text in the diagnostic DB error field.
            self.s.rpc.call('fail_gmail_notification', **params, p_error_code='GMAIL_SYNC_FAILED', p_retry=True)
            # Backoff is bounded, interruptible, and does not acknowledge unpersisted work.
            self.stop.wait(min(60, 2 ** min(claim.get('attempt_count', 1), 5)))
            return True

    def maintenance(self):
        now = time.monotonic()
        # Analysis lease recovery must work even when Gmail/watch is unavailable.
        if not self.last_recovery or now - self.last_recovery >= self.s.settings.worker_sync_seconds:
            self.s.rpc.call('recover_expired_analyses')
            self.last_recovery = now
        if not self.last_watch or now - self.last_watch >= 86400:
            watch = self.s.gmail.watch()
            self.s.rpc.call('configure_gmail_mailbox', p_email=self.s.settings.gmail_email,
                            p_watch_expires_at=datetime.fromtimestamp(int(watch['expiration']) / 1000, timezone.utc))
            self.last_watch = now
        if not self.last_sync or now - self.last_sync >= self.s.settings.worker_sync_seconds:
            profile = self.s.gmail.profile()
            self.s.rpc.call('enqueue_gmail_notification', p_subscription=self.s.settings.gmail_subscription,
                            p_pubsub_message_id='reconcile-' + str(int(time.time())), p_email=self.s.settings.gmail_email,
                            p_history_id=str(profile['historyId']))
            # Resume classification interrupted after successful durable registration.
            rows = self.s.client.table('work_items').select('work_id,subject,classification_token').eq('queue_status', 'PENDING').is_('classification_reason', 'null').order('received_at').limit(25).execute().data
            for row in rows:
                source = self.s.source(row['work_id'])
                self.s.verify_source(row['work_id'], source)
                result = classify(self.s.agent, row['subject'], source['mail_body'])
                try:
                    self.s.rpc.call('finish_classification', p_work_id=row['work_id'], p_classification_token=row['classification_token'],
                                    p_status=result.status, p_reason=result.reason)
                except Exception:
                    logger.warning('classification resume rejected')
            self.last_sync = now

    def start(self):
        def loop():
            self.running = True
            try:
                while not self.stop.is_set():
                    try:
                        self.maintenance()
                    except Exception:
                        logger.warning('worker maintenance failed')
                    try:
                        if self.process_once():
                            continue
                    except Exception:
                        logger.warning('worker operation failed')
                    self.stop.wait(self.s.settings.worker_poll_seconds)
            finally:
                self.running = False
        self.thread = threading.Thread(target=loop, daemon=True, name='gmail-worker')
        self.thread.start()

    def close(self):
        self.stop.set()
        if self.thread:
            self.thread.join(timeout=5)
