"""Durable, extraction-only background execution; never logs mail/model payloads."""
import logging
import threading
import time

from langsmith import tracing_context
from app.agents.agents import AgentRunner, chat_model
from app.agents.graph import build_graph, run_case
from app.agents.nodes import Nodes
from app.agents.schemas import AgentRole, RunCaseInput, StartFrom
from app.core.errors import ServiceError
from app.core.leases import LeaseHeartbeat
from app.features.mail.infrastructure.attachments import AttachmentRepository
from app.features.work_cases.application.extraction_tools import CURRENT_SCOPE, ExtractionScope

logger = logging.getLogger('uvicorn.error.case_extraction')


class ExtractionWorker:
    def __init__(self, settings, repo, mailbox, *, runner_factory=None, attachment_repo=None):
        self.settings, self.repo, self.mailbox = settings, repo, mailbox
        self.runner_factory = runner_factory or (lambda: AgentRunner(chat_model(settings)))
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
                    deadline=time.monotonic() + 60, lease_check=lease.check)
                token = CURRENT_SCOPE.set(scope)
                try:
                    with tracing_context(enabled=False):
                        runner = self.runner_factory()

                        def guarded_runner(role, data):
                            if role != AgentRole.INFO_EXTRACTOR:
                                raise ServiceError('EXTRACTION_ONLY', '후속 역할은 아직 실행하지 않습니다.', 409)
                            scope.check(data.case_id, data.input_revision)
                            output = runner(role, data)
                            scope.check(data.case_id, data.input_revision)
                            if output.next_action == 'STORED':
                                if scope.result is None:
                                    raise ValueError('RESULT_NOT_STAGED')
                                # The staged, server-validated fields are authoritative.
                                output = output.model_copy(update={'unknown_fields': scope.result.unknown_fields,
                                                                   'quote_version_ids': []})
                            return output

                        graph = build_graph(guarded_runner,
                            lambda _: self.repo.revision(scope.owner, scope.case_id),
                            Nodes(preprocess=lambda _: {}), stop_after_extraction=True)
                        output = run_case(graph, RunCaseInput(case_id=scope.case_id, start_from=StartFrom.START,
                                                             input_revision=scope.revision, mail_ids=list(scope.mail_ids)))
                        lease.check()
                        if output.status == 'FAILED':
                            self.repo.fail(job, 'EXTRACTION_FAILED')
                        elif output.status == 'STALE':
                            self.repo.fail(job, 'INPUT_REVISION_CHANGED')
                        else:
                            result = self.repo.finish(job, output, scope.result, self.settings.llm_model)
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
