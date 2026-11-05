"""Graph START's isolated durable queue and service-only transaction boundaries."""
from app.core.errors import ServiceError
from app.features.work_cases.infrastructure.runtime_repository import CaseRuntimeRepository


class GraphRuntimeRepository(CaseRuntimeRepository):
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

    def action(self, owner, case_id, revision, action, key, body_hash, request_id):
        if action != 'START':
            return super().action(owner, case_id, revision, action, key, body_hash, request_id)
        return self.call('submit_case_graph_start', p_owner=str(owner), p_case=str(case_id), p_revision=revision,
                         p_key=key, p_hash=body_hash, p_request_id=request_id)

    def resume_available(self):
        # Extraction-only resume jobs must never be silently consumed as graphs.
        return False

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
        # Do not assume an unagreed review claim RPC also owns START jobs.
        return self.call('claim_case_graph_run', p_start_from='START')

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
        # REPLY/PLAN scheduling/claim contract must be agreed with DB owner
        # before enabling consumers; START never falls back to a review claim.
        return result

    def finish_graph(self, job, output):
        name = 'complete_case_graph_review' if job.get('execution_mode') == 'GRAPH_REVIEW_V1' else 'complete_case_graph_run'
        return self.call(name, p_job=job['job_id'], p_token=job['claim_token'],
                         p_revision=job['input_revision'], p_batch=job['batch_id'], p_output=output.model_dump(mode='json'))


def runtime_repository(client, settings):
    return GraphRuntimeRepository(client)
