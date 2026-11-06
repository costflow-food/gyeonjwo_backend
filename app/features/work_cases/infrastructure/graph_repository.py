"""Graph START's isolated durable queue and service-only transaction boundaries."""
import logging

from app.core.errors import ServiceError
from app.features.work_cases.infrastructure.runtime_repository import CaseRuntimeRepository


logger = logging.getLogger('uvicorn.error.case_graph')


class GraphRuntimeRepository(CaseRuntimeRepository):
    REVIEW_CAPABILITIES = {'REPLY', 'PLAN', 'REVIEW_STORES', 'REVIEW_CARD_STAGE',
                           'REVIEW_CARD_COMMIT', 'REVIEW_OUTBOUND_GUARD'}

    def __init__(self, client):
        super().__init__(client)
        self.review_ready = False
        self.prefer_review = True

    def available(self):
        result = self.call('case_graph_health')
        return self.health_ready(result)

    @staticmethod
    def health_ready(result):
        required = {'START', 'SCOPED_LEASES', 'DOCUMENTS', 'CANDIDATES', 'DRAFTS', 'RUNS', 'RESULTS',
                    'LEGACY_RFQ_GUARD', 'CAUTION_GUARD', 'CANDIDATE_SETS', 'USER_SELECTION_GATE', 'EXTRACTED_CONTINUATIONS'}
        return (isinstance(result, dict) and type(result.get('contract_version')) is int and result.get('contract_version') == 1
                and result.get('ready') is True and result.get('legacy_queue_drained') is True
                and isinstance(result.get('capabilities'), list)
                and all(isinstance(item, str) for item in result['capabilities'])
                and required <= set(result.get('capabilities') or []))

    def action(self, owner, case_id, revision, action, key, body_hash, request_id, *, conditions=None):
        if action != 'START':
            return super().action(owner, case_id, revision, action, key, body_hash, request_id)
        options = {'p_conditions': conditions} if conditions else {}
        try:
            return self.call('submit_case_graph_start', p_owner=str(owner), p_case=str(case_id), p_revision=revision,
                             p_key=key, p_hash=body_hash, p_request_id=request_id, **options)
        except ServiceError as error:
            if not conditions or error.code != 'EXTRACTION_SCHEMA_REQUIRED':
                raise
            raise ServiceError('START_CONDITIONS_INTEGRATION_REQUIRED', '시작 조건 저장 기능이 아직 준비되지 않았습니다.',
                               503, retryable=False) from None

    def resume_available(self):
        health = self.call('case_graph_health')
        return (self.health_ready(health) and 'GRAPH_RESUME' in health['capabilities']
                and super().resume_available())

    def context(self, owner, case_id, revision, batch):
        try:
            rows = (self.client.table('work_cases').select(
                'id,initial_mail_id,mailbox_ref,input_revision,current_batch_id,status,processing_step,archived_at,decision,'
                'item_name,specification,quantity::text,unit,due_date,mandatory_conditions_jsonb,policy_jsonb')
                .eq('owner_user_id', str(owner)).eq('id', str(case_id)).limit(1).execute().data)
            if not isinstance(rows, list) or len(rows) > 1:
                raise ValueError('Invalid context')
        except Exception:
            raise ServiceError('PROCESSING_UNAVAILABLE', '그래프 업무 정보를 확인할 수 없습니다.', 503) from None
        if not rows:
            raise ServiceError('RESOURCE_NOT_FOUND', '업무를 찾을 수 없습니다.', 404)
        row = rows[0]
        if row['input_revision'] != revision or str(row.get('current_batch_id')) != str(batch):
            raise ServiceError('REVISION_CONFLICT', '검토 입력이 변경됐습니다.', 409)
        # Graph role steps as written by begin_case_graph_agent_run (DB work_cases_check5):
        # EXTRACTING, SELECTING_CANDIDATES (candidate role running), GENERATING_PLANS (roles 3-5).
        # CANDIDATES is the post-graph user-selection wait, not a running graph step.
        if (row.get('archived_at') or row.get('decision') in {'ACCEPT_INCREASE', 'CONFIRM_REVIEW'} or row['status'] != 'Running'
                or row['processing_step'] not in {'EXTRACTING', 'SELECTING_CANDIDATES', 'GENERATING_PLANS'}):
            raise ServiceError('ACTION_NOT_ALLOWED', '그래프 검토 실행이 중단됐습니다.', 409)
        return row

    def claim(self):
        review_first = self.review_ready and self.prefer_review
        if self.review_ready:
            self.prefer_review = not self.prefer_review
        if review_first:
            job = self.claim_review()
            if job is not None:
                return job
        job = self.call('claim_case_graph_run', p_start_from='START')
        if job is None and self.review_ready and not review_first:
            return self.claim_review()
        return job

    def claim_review(self):
        try:
            return self.call('claim_case_graph_review_run')
        except ServiceError:
            self.review_ready = False
            logger.warning('review claim unavailable code=REVIEW_RPC_UNAVAILABLE')
            raise

    def heartbeat(self, job):
        if self.call('heartbeat_case_graph_run', p_job=job['job_id'], p_token=job['claim_token']) is not True:
            raise ServiceError('EXTRACTION_LEASE_LOST', '그래프 실행 소유권이 변경됐습니다.', 409)

    def fail(self, job, code):
        return self.call('fail_case_graph_run', p_job=job['job_id'], p_token=job['claim_token'], p_code=code)

    def recover(self):
        result = self.call('recover_case_graph_runs')
        # Explicit graph-mode rollout may continue already-authorized Running
        # extraction-only work. Never auto-start Waiting/Confirm/Pending/archive.
        scheduled = self.call('schedule_case_graph_continuations', p_limit=25)
        if type(scheduled) is not int or not 0 <= scheduled <= 25:
            raise ServiceError('PROCESSING_UNAVAILABLE', '추출 완료 업무의 그래프 연결을 확인할 수 없습니다.', 503)
        self.review_ready = False
        try:
            health = self.call('case_graph_health')
        except ServiceError:
            return result
        if self.health_ready(health) and 'ORPHAN_RECOVERY' in health['capabilities']:
            try:
                count = self.call('recover_orphaned_case_graph_runs', p_limit=25)
                if type(count) is not int or not 0 <= count <= 25:
                    raise ValueError('Invalid recovery acknowledgement')
            except (ServiceError, ValueError):
                logger.warning('orphan graph recovery unavailable code=ORPHAN_RECOVERY_FAILED')
        if self.health_ready(health) and self.REVIEW_CAPABILITIES <= set(health['capabilities']):
            try:
                for rpc in ('schedule_case_graph_replies', 'finalize_case_graph_reviews'):
                    count = self.call(rpc, p_limit=25)
                    if type(count) is not int or not 0 <= count <= 25:
                        raise ServiceError('PROCESSING_UNAVAILABLE', '회신 검토 예약 상태를 확인할 수 없습니다.', 503)
                self.review_ready = True
            except ServiceError:
                logger.warning('review queue unavailable code=REVIEW_RPC_UNAVAILABLE')
        return result

    def finish_graph(self, job, output):
        name = 'complete_case_graph_review' if job.get('execution_mode') == 'GRAPH_REVIEW_V1' else 'complete_case_graph_run'
        payload = output.model_dump(mode='json')
        # The role-finish RPC receives the private original separately. Do not
        # duplicate it in the terminal payload that can feed progress displays.
        for step in payload['steps']:
            if step['next_action'] == 'HOLD':
                original = step['reason']
                step['reason'] = '검토를 보류했어요.'
                payload['unresolved'] = [value for value in payload['unresolved']
                                         if original not in value]
        return self.call(name, p_job=job['job_id'], p_token=job['claim_token'],
                         p_revision=job['input_revision'], p_batch=job['batch_id'], p_output=payload)


def runtime_repository(client, settings):
    return GraphRuntimeRepository(client)
