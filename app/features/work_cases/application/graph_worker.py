"""그래프 작업 큐 소비자. GRAPH_START_V1(인상 통보로 시작)과 GRAPH_REVIEW_V1(회신·구매안 다시 검토)을 처리한다."""
import logging
import time
from uuid import UUID
from app.core.graph_timing import graph_timings
from app.core.tracing import agent_tracing
from app.core.errors import ServiceError
from app.core.leases import LeaseHeartbeat
from app.shared.infrastructure.db.contracts import OwnershipLost
from app.features.work_cases.application.extraction_scope import CURRENT_SCOPE, ExtractionScope
from app.features.work_cases.application.extraction_worker import ExtractionWorker
from app.features.work_cases.application.graph_boundary import graph_contract, run_graph
from app.core.logs import error_origin

logger = logging.getLogger('uvicorn.error.case_graph')


def input_changed(repo, job):
    """job을 예약한 뒤 업무 입력(revision·batch)이 바뀌었는지. 확인하지 못하면 False(원래 코드로 기록한다)."""
    check = getattr(repo, 'input_changed', None)
    if check is None:
        return False
    try:
        return check(job) is True
    except Exception:
        return False


class GraphWorker(ExtractionWorker):
    def __init__(self, *args, on_drafts_ready=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.on_drafts_ready = on_drafts_ready

    def load_adapter(self):
        graph_contract()
        return run_graph

    def record_failure(self, job, error):
        """실패한 그래프 job을 DB에 기록하고, 기록한 코드를 돌려준다."""
        code = 'GRAPH_FAILED'
        if isinstance(error, OwnershipLost):
            # 임대를 잃어 실행을 멈춘 경우. 에이전트·그래프 오류(GRAPH_FAILED)와 구분해 WORKER_INTERRUPTED로 남긴다
            code = 'WORKER_INTERRUPTED'
        elif isinstance(error, ServiceError):
            code = {'REVISION_CONFLICT': 'INPUT_REVISION_CHANGED', 'EXTRACTION_TIME_LIMIT': 'GRAPH_TIME_LIMIT',
                    'WORKER_INTERRUPTED': 'WORKER_INTERRUPTED'}.get(error.code, 'GRAPH_FAILED')
        if (isinstance(error, OwnershipLost) or code == 'GRAPH_FAILED') and input_changed(self.repo, job):
            # 실행 중 새 회신 등으로 입력이 바뀐 경우(KAN-336). DB의 임대 갱신(heartbeat_case_graph_run)은 revision이
            # 바뀌어도 false를 돌려줘 임대 상실과 구분되지 않고, 역할 안에서 난 REVISION_CONFLICT도 그래프가 일반 실패로
            # 바꾼다. 실패가 아니라 STALE로 남겨야 통계가 맞고, 이 job이 읽지 못한 회신을 다음 예약이 다시 넘긴다
            # (DB internal.stale_reply_needs_retry는 STALE·INPUT_REVISION_CHANGED인 job의 회신만 다시 넘긴다. KAN-338)
            logger.info('graph input changed during run; recorded as INPUT_REVISION_CHANGED instead of %s job_id=%s',
                        code, job.get('job_id'))
            code = 'INPUT_REVISION_CHANGED'
        recorded = self.repo.fail(job, code)
        if isinstance(error, OwnershipLost) and code == 'WORKER_INTERRUPTED' and recorded is not True:
            # fail_case_graph_run은 DB 임대가 아직 남아 있으면 WORKER_INTERRUPTED를 받지 않는다(false). 상실을 판단한
            # 시점(갱신 실패 60초·DB가 소유권 변경을 알림)에는 임대가 대개 남아 있어, 그대로 두면 만료 뒤
            # recover_case_graph_runs가 정리할 때까지 최대 약 2분 job이 PROCESSING으로 남아 같은 업무의 회신 검토 예약이
            # 막힌다. 바로 닫을 수 있게 GRAPH_FAILED로 다시 기록한다(이미 다른 워커가 가져갔다면 DB가 토큰으로 거절한다).
            # 그래서 임대 상실은 DB에 대개 GRAPH_FAILED로 남는다. 상실 사유(OWNERSHIP_LOST·RETRY_EXHAUSTED)는
            # 같은 job_id의 'lease renewal failed' 로그로 구분한다
            code = 'GRAPH_FAILED'
            accepted = self.repo.fail(job, code)
            logger.warning('graph lease lost; WORKER_INTERRUPTED not accepted, retried as GRAPH_FAILED accepted=%s '
                           'job_id=%s', accepted is True, job.get('job_id'))
        return code

    def process_once(self):
        claim_started = time.monotonic()
        job = self.repo.claim()
        claimed = time.monotonic()
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
        logger.info('graph claimed job_id=%s start_from=%s claim_ms=%.1f',
                    job['job_id'], mode, (claimed - claim_started) * 1000)
        scope = None
        timings = dict(db=0.0, llm=0.0, document=0.0)
        timing_token = graph_timings.set(timings)
        try:
            with LeaseHeartbeat(lambda: self.repo.heartbeat(job), seconds=10, worker='graph',
                                job_id=job['job_id']) as lease:
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
            code = self.record_failure(job, error)
            logger.warning('graph failed code=%s job_id=%s case_id=%s start_from=%s %s', code, job.get('job_id'),
                           job.get('case_id'), mode, error_origin(error))
        finally:
            graph_timings.reset(timing_token)
            logger.info('graph execution ended job_id=%s start_from=%s queue_wait_ms=%s execution_ms=%.1f '
                        'db_ms=%.1f llm_ms=%.1f document_ms=%.1f', job['job_id'], mode,
                        job.get('queue_wait_ms', 'unavailable'), (time.monotonic() - claimed) * 1000,
                        timings['db'], timings['llm'], timings['document'])
            if scope is not None:
                scope.blocked = True
        return True
