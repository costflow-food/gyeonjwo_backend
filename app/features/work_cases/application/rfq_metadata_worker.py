"""Collect only sent-mail headers, under DB leases; never send or read bodies."""
import logging
import threading
from datetime import datetime, timezone
from email.utils import getaddresses
from app.core.errors import ServiceError

logger = logging.getLogger('uvicorn.error.rfq_metadata')


def outbound_metadata(raw):
    headers = {str(h['name']).casefold(): str(h['value'])
               for h in raw.get('payload', {}).get('headers', [])}
    return {
        'gmail_message_id': raw['id'], 'gmail_thread_id': raw.get('threadId'),
        'message_id_header': headers.get('message-id'),
        'received_at': datetime.fromtimestamp(int(raw['internalDate']) / 1000, timezone.utc).isoformat().replace('+00:00', 'Z'),
        'sender': [address.casefold() for _, address in getaddresses([headers.get('from', '')]) if address],
        'recipients': [address.casefold() for _, address in getaddresses([headers.get('to', ''), headers.get('cc', '')]) if address],
        'subject': headers.get('subject', ''),
    }


class RfqMetadataWorker:
    def __init__(self, repo, mailbox, owners):
        # owners is called every cycle so newly connected mailboxes are picked up without a restart.
        self.repo, self.mailbox, self.owners = repo, mailbox, owners
        self.stop = threading.Event()
        self.thread = None
        self.running = False

    def process_owner(self, owner):
        # Claim one at a time: a slow provider cannot expire leases of queued jobs.
        jobs = self.repo.claim_metadata(owner, limit=1)
        for job in jobs:
            try:
                connection, grant = self.mailbox.authorized(owner, 'read')
                if connection['mailbox_ref'] != job['mailbox_ref']:
                    raise ServiceError('MAILBOX_RECONNECT_REQUIRED', '메일함 연결을 확인해 주세요.', 409)
                raw = self.mailbox.google.message_headers(grant, job['provider_message_id'])
                metadata = outbound_metadata(raw)
                if metadata['gmail_message_id'] != job['provider_message_id']:
                    raise ValueError('Provider message identity mismatch')
                self.repo.complete_metadata(owner, job, metadata)
                logger.info('rfq outbound_metadata_saved')
            except Exception:
                # Retry timing and attempt ceiling are owned by the SQL function.
                # A lost completion acknowledgement is safe: READY cannot be failed.
                self.repo.fail_metadata(owner, job)
                logger.warning('rfq outbound_metadata_failed code=METADATA_UNAVAILABLE')
        return bool(jobs)

    def start(self):
        def loop():
            self.running = True
            try:
                while not self.stop.is_set():
                    busy = False
                    try:
                        owners = self.owners()
                    except Exception:
                        owners = ()
                        logger.warning('rfq metadata_worker_unavailable code=STORAGE_UNAVAILABLE')
                    for owner in owners:
                        if self.stop.is_set():
                            break
                        try:
                            busy = self.process_owner(owner) or busy
                        except Exception:
                            logger.warning('rfq metadata_worker_unavailable code=STORAGE_UNAVAILABLE')
                    self.stop.wait(1 if busy else 15)
            finally:
                self.running = False
        self.thread = threading.Thread(target=loop, name='rfq-metadata', daemon=True)
        self.thread.start()

    def close(self):
        self.stop.set()
        if self.thread:
            self.thread.join(timeout=25)
