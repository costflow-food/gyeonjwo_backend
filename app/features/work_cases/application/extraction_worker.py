"""Durable, extraction-only background execution; never logs mail/model payloads."""
import logging
import threading
import time

from langsmith import tracing_context
from app.features.work_cases.application.agent_boundary import load_extraction_adapter
from app.features.work_cases.domain.extraction_outcome import ExtractionOutcome
from app.core.errors import ServiceError
from app.core.leases import LeaseHeartbeat
from app.features.mail.infrastructure.attachments import AttachmentRepository
from app.features.work_cases.application.extraction_scope import CURRENT_SCOPE, ExtractionScope

logger = logging.getLogger('uvicorn.error.case_extraction')


class ExtractionWorker:
    def __init__(self, settings, repo, mailbox, *, runner_factory=None, attachment_repo=None):
        self.settings, self.repo, self.mailbox = settings, repo, mailbox
        self.runner_factory = runner_factory
        self.adapter = None
        self.attachment_repo = attachment_repo or AttachmentRepository(repo.client)
        self.stop, self.wake = threading.Event(), threading.Event()
        self.thread = None
        self.running = False

    def process_once(self):
        job = self.repo.claim()
        if job is None:
            return False
        if not isinstance(job, dict) or any(k not in job for k in (
                'owner_user_id', 'case_id', 'input_revision', 'batch_id', 'job_id', 'claim_token', 'mail_ids')):
            raise ServiceError('PROCESSING_UNAVAILABLE', '실행 접수 결과를 확인할 수 없습니다.', 503)
        logger.info('extraction started role=INFO_EXTRACTOR')
        try:
            with LeaseHeartbeat(lambda: self.repo.heartbeat(job), seconds=10) as lease:
                scope = ExtractionScope(owner=job['owner_user_id'], case_id=str(job['case_id']),
                    revision=job['input_revision'], batch_id=str(job['batch_id']), mail_ids=tuple(job['mail_ids']),
                    repo=self.repo, mailbox=self.mailbox, attachment_repo=self.attachment_repo,
                    max_characters=self.settings.max_document_characters,
                    deadline=time.monotonic() + 60, lease_check=lease.check, job=job)
                token = CURRENT_SCOPE.set(scope)
                try:
                    with tracing_context(enabled=False):
                        adapter = self.runner_factory() if self.runner_factory else self.adapter
                        if adapter is None:
                            adapter = load_extraction_adapter()
                        raw = adapter(settings=self.settings, scope=scope)
                        output = ExtractionOutcome.model_validate(
                            raw.model_dump(mode='json') if hasattr(raw, 'model_dump') else raw)
                        scope.check(str(output.case_id), output.input_revision)
                        if output.status in {'EXTRACTION_READY', 'NEEDS_INFO'} and not scope.full_quote_ids:
                            raise ValueError('QUOTE_RESULTS_NOT_PERSISTED')
                        lease.check()
                        if output.status == 'FAILED':
                            self.repo.fail(job, 'EXTRACTION_FAILED')
                        elif output.status == 'STALE':
                            self.repo.fail(job, 'INPUT_REVISION_CHANGED')
                        else:
                            result = self.repo.finish_agent(job, output, scope.full_quote_ids, scope.agent_metadata)
                            if not isinstance(result, dict) or result.get('kind') not in {'STORED', 'STALE'}:
                                raise ServiceError('PROCESSING_UNAVAILABLE', '추출 결과 저장을 확인할 수 없습니다.', 503)
                finally:
                    CURRENT_SCOPE.reset(token)
            logger.info('extraction finished role=INFO_EXTRACTOR')
        except Exception as error:
            code = 'INPUT_REVISION_CHANGED' if isinstance(error, ServiceError) and error.code == 'REVISION_CONFLICT' else 'EXTRACTION_FAILED'
            # The DB token/revision guard prevents this from changing newer work.
            self.repo.fail(job, code)
            logger.warning('extraction failed code=%s', code)
        return True

    def start(self):
        if not self.runner_factory:
            self.adapter = load_extraction_adapter()
        if not self.repo.available():
            raise ServiceError('EXTRACTION_SCHEMA_REQUIRED', '실행 DB 계약이 준비되지 않았습니다.', 503)
        self.repo.recover()  # Only expired durable leases -> FAILED, never Complete.

        def loop():
            last_recovery = 0
            self.running = True
            try:
                while not self.stop.is_set():
                    try:
                        # Startup can precede expiry of the interrupted lease.
                        if time.monotonic() - last_recovery >= 10:
                            self.repo.recover()
                            last_recovery = time.monotonic()
                        if self.process_once():
                            continue
                    except Exception:
                        logger.warning('extraction worker unavailable code=STORAGE_UNAVAILABLE')
                    self.wake.wait(5)
                    self.wake.clear()
            finally:
                self.running = False
        self.thread = threading.Thread(target=loop, daemon=True, name='case-extraction')
        self.thread.start()

    def close(self):
        self.stop.set()
        self.wake.set()
        if self.thread:
            self.thread.join(timeout=25)
        # A still-running process is not marked successful. Expired leases are
        # recovered by the next worker; do not mark another server's job failed.
