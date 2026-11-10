"""Collect only sent-mail headers, under DB leases; never send or read bodies."""
import logging
import threading
import time
from datetime import datetime, timezone
from email.utils import getaddresses
from app.core.errors import ServiceError

logger = logging.getLogger('uvicorn.error.rfq_metadata')
# 보완 질문·역제안 발송 기록 재시도: 실패마다 간격을 두 배로(최대 CASE_RETRY_MAX초), CASE_ATTEMPTS번 실패하면 멈춘다
CASE_RETRY_BASE, CASE_RETRY_MAX, CASE_ATTEMPTS = 30, 600, 6


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
        self.case_retries = {}       # draft_id → (실패 횟수, 다음 시도 시각). 프로세스 안에서만 기억한다

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

    def process_case_drafts(self, owner):
        """보완 질문·역제안 발송 메일의 헤더를 읽어 발송 기록(OUT 메일 행)을 남긴다. RFQ와 달리 DB 큐가 없어
        발송 기록이 없는 SENT 초안을 직접 찾는다. 기록이 생기면 업체 답장이 In-Reply-To로 업무에 연결된다.
        한 주기에 한 건만 처리한다. 반환: 처리한 초안이 있으면 True."""
        now = time.monotonic()
        pending = [row for row in self.repo.case_metadata_drafts(owner)
                   if self.case_retries.get(row['draft_id'], (0, 0))[0] < CASE_ATTEMPTS
                   and self.case_retries.get(row['draft_id'], (0, 0))[1] <= now]
        if not pending:
            return False
        row = pending[0]
        try:
            _, grant = self.mailbox.authorized(owner, 'read')
            raw = self.mailbox.google.message_headers(grant, row['provider_message_id'])
            metadata = outbound_metadata(raw)
            if metadata['gmail_message_id'] != row['provider_message_id']:
                raise ValueError('Provider message identity mismatch')
            self.repo.complete_case_metadata(owner, row['draft_id'], metadata)
            self.case_retries.pop(row['draft_id'], None)
            logger.info('case outbound_metadata_saved')
        except Exception as error:
            attempts = self.case_retries.get(row['draft_id'], (0, 0))[0] + 1
            delay = min(CASE_RETRY_MAX, CASE_RETRY_BASE * 2 ** (attempts - 1))
            self.case_retries[row['draft_id']] = (attempts, now + delay)
            code = getattr(error, 'code', None) or type(error).__name__
            logger.warning('case outbound_metadata_failed attempts=%s cause=%s', attempts, code)
        return True

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
                        try:
                            busy = self.process_case_drafts(owner) or busy
                        except Exception:
                            logger.warning('case metadata_worker_unavailable code=STORAGE_UNAVAILABLE')
                    self.stop.wait(1 if busy else 15)
            finally:
                self.running = False
        self.thread = threading.Thread(target=loop, name='rfq-metadata', daemon=True)
        self.thread.start()

    def close(self):
        self.stop.set()
        if self.thread:
            self.thread.join(timeout=25)
