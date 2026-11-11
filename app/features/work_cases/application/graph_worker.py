"""그래프 작업 큐 소비자. GRAPH_START_V1(인상 통보로 시작)과 GRAPH_REVIEW_V1(회신·구매안 다시 검토)을 처리한다."""
import logging
import time
from uuid import UUID
from app.core.tracing import agent_tracing
from app.core.errors import ServiceError
from app.core.leases import LeaseHeartbeat
from app.features.work_cases.application.extraction_scope import CURRENT_SCOPE, ExtractionScope
from app.features.work_cases.application.extraction_worker import ExtractionWorker
from app.features.work_cases.application.graph_boundary import graph_contract, run_graph
from app.core.logs import error_origin

logger = logging.getLogger('uvicorn.error.case_graph')


class GraphWorker(ExtractionWorker):
    def __init__(self, *args, on_drafts_ready=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.on_drafts_ready = on_drafts_ready

    def load_adapter(self):
        graph_contract()
        return run_graph

    def process_once(self):
        job = self.repo.claim()
        if job is None:
            return False
        try:
            for key in ('owner_user_id', 'case_id', 'input_revision', 'batch_id', 'job_id', 'claim_token', 'mail_ids'):
                if key not in job:
                    raise ValueError('Invalid job')
            for key in ('owner_user_id', 'case_id', 'batch_id', 'job_id', 'claim_token'):
                UUID(str(job[key]))
            review = job.get('execution_mode') == 'GRAPH_REVIEW_V1'
            mode = job.get('start_from')
            valid_mode = (job.get('execution_mode') == 'GRAPH_START_V1' and mode == 'START') or (
                review and mode in {'REPLY', 'PLAN'})
            if (not valid_mode
                    or type(job['input_revision']) is not int or job['input_revision'] < 1
                    or not isinstance(job['mail_ids'], list) or len(job['mail_ids']) > 100
                    or (mode == 'START' and len(job['mail_ids']) != 1)
                    or (mode == 'REPLY' and not job['mail_ids'])
                    or (mode == 'PLAN' and job['mail_ids'])):
                raise ValueError('Invalid graph job contract')
            for mail_id in job['mail_ids']:
                UUID(str(mail_id))
            if review:
                evidence = job.get('evidence_mail_ids')
                if (not isinstance(evidence, list) or not 1 <= len(evidence) <= 500
                        or len(evidence) != len(set(evidence)) or not set(job['mail_ids']) <= set(evidence)):
                    raise ValueError('Invalid review evidence scope')
                for mail_id in evidence:
                    UUID(str(mail_id))
        except (ValueError, KeyError, TypeError):
            # Do not fail/release a malformed or another queue's claimed token.
            raise ServiceError('PROCESSING_UNAVAILABLE', '그래프 작업 계약을 확인할 수 없습니다.', 503) from None
        logger.info('graph started start_from=%s', mode)
        scope = None
        try:
            with LeaseHeartbeat(lambda: self.repo.heartbeat(job), seconds=10) as lease:
                def active():
                    lease.check()
                    if self.stop.is_set():
                        raise ServiceError('WORKER_INTERRUPTED', '검토 워커가 중단됐습니다.', 503)
                scope = ExtractionScope(owner=str(job['owner_user_id']), case_id=str(job['case_id']),
                    revision=job['input_revision'], batch_id=str(job['batch_id']), mail_ids=tuple(job['mail_ids']),
                    repo=self.repo, mailbox=self.mailbox, attachment_repo=self.attachment_repo,
                    max_characters=self.settings.max_document_characters,
                    deadline=time.monotonic() + (self.settings.agent_graph_review_timeout_seconds if review else self.settings.agent_graph_timeout_seconds), lease_check=active, job=job)
                case = scope.check(scope.case_id, scope.revision)
                if mode == 'START' and scope.mail_ids != (str(case['initial_mail_id']),):
                    raise ValueError('Invalid START source')
                if review and str(case['initial_mail_id']) not in job['evidence_mail_ids']:
                    raise ValueError('Review initial source missing')
                token = CURRENT_SCOPE.set(scope)
                try:
                    with agent_tracing(self.settings, case_id=scope.case_id, job_id=job['job_id'],
                                       input_revision=scope.revision, start_from=mode):
                        output = (self.adapter or run_graph)(settings=self.settings, scope=scope,
                                                             runner_factory=self.runner_factory)
                    from app.agents.schemas import RunCaseOutput
                    output = RunCaseOutput.model_validate(output.model_dump(mode='json'))
                    if str(output.case_id) != scope.case_id or output.input_revision != scope.revision:
                        raise ValueError('Changed graph result scope')
                    if str(output.status) != 'STALE':
                        scope.check(scope.case_id, scope.revision)
                    if not review and (str(output.status) == 'COMPLETE' or output.card_id is not None):
                        raise ValueError('START cannot complete a review')
                    if review and str(output.status) == 'COMPLETE' and (not getattr(scope.agent_store, 'card_id', None)
                            or output.card_id != scope.agent_store.card_id):
                        raise ValueError('Unconfirmed staged review card')
                    if str(output.status) in {'FAILED', 'STALE'}:
                        self.repo.fail(job, 'INPUT_REVISION_CHANGED' if str(output.status) == 'STALE' else 'GRAPH_FAILED')
                    else:
                        result = self.repo.finish_graph(job, output)
                        if not isinstance(result, dict) or result.get('kind') not in ({'STORED', 'STALE', 'WAITING_OUTBOUND'} if review else {'STORED', 'STALE'}):
                            raise ValueError('Graph completion not confirmed')
                        if review and result['kind'] in {'STORED', 'WAITING_OUTBOUND'}:
                            if str(output.status) == 'COMPLETE':
                                if result.get('card_id') != output.card_id:
                                    raise ValueError('Review card completion mismatch')
                                if result['kind'] == 'STORED' and (type(result.get('review_revision')) is not int
                                        or result['review_revision'] < 1 or result.get('status') != 'Complete'
                                        or result.get('input_revision') != scope.revision):
                                    raise ValueError('Unconfirmed final review commit')
                            if output.draft_ids and self.on_drafts_ready:
                                self.on_drafts_ready()
                        if not review and result['kind'] == 'STORED' and str(output.status) == 'WAITING_REPLY':
                            if result.get('selection_required') is not True:
                                raise ValueError('Missing user selection gate')
                            UUID(str(result['candidate_set_id']))
                            if str(result['candidate_set_id']) != getattr(scope.agent_store, 'candidate_set_id', None):
                                raise ValueError('Candidate set scope mismatch')
                            # Do not wake sender here. API-12 does so only after
                            # selected-only publication and policy validation.
                finally:
                    CURRENT_SCOPE.reset(token)
            logger.info('graph finished start_from=%s', mode)
        except Exception as error:
            code = 'GRAPH_FAILED'
            if isinstance(error, ServiceError):
                code = {'REVISION_CONFLICT': 'INPUT_REVISION_CHANGED', 'EXTRACTION_TIME_LIMIT': 'GRAPH_TIME_LIMIT',
                        'WORKER_INTERRUPTED': 'WORKER_INTERRUPTED'}.get(error.code, 'GRAPH_FAILED')
            self.repo.fail(job, code)
            logger.warning('graph failed code=%s job_id=%s case_id=%s start_from=%s %s', code, job.get('job_id'),
                           job.get('case_id'), mode, error_origin(error))
        finally:
            if scope is not None:
                scope.blocked = True
        return True
