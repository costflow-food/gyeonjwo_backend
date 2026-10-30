"""One mutation RPC per transaction. No multi-request REST pseudo-transactions.

RPCs in integration_contracts/extraction_runtime.json must be implemented by the
DB owner before enabling this feature. This adapter never installs SQL.
"""
from app.core.errors import ServiceError


class CaseRuntimeRepository:
    def __init__(self, client):
        self.client = client

    def call(self, name, **params):
        try:
            result = self.client.rpc(name, params).execute().data
        except Exception as error:
            if getattr(error, 'code', None) == 'PGRST202':
                raise ServiceError('EXTRACTION_SCHEMA_REQUIRED', '업무 실행·저장 DB 계약을 먼저 적용해야 합니다.', 503,
                                   retryable=False, next_action='CONTACT_SERVER_ADMIN') from None
            raise ServiceError('PROCESSING_UNAVAILABLE', '업무 실행 저장소를 확인할 수 없습니다.', 503) from None
        if isinstance(result, dict) and result.get('kind') == 'ERROR':
            codes = {'RESOURCE_NOT_FOUND': 404, 'WORK_ALREADY_STARTED': 409, 'REVISION_CONFLICT': 409,
                     'ACTION_NOT_ALLOWED': 409, 'REQUEST_IN_PROGRESS': 409, 'IDEMPOTENCY_KEY_REUSED': 409}
            code = result.get('code')
            if code in codes:
                raise ServiceError(code, '최신 업무 상태와 요청을 확인해 주세요.', codes[code],
                                   current_revision=result.get('current_revision'), retryable=False)
            raise ServiceError('PROCESSING_UNAVAILABLE', '업무 변경을 확인할 수 없습니다.', 503)
        return result

    def available(self):
        result = self.call('case_extraction_health')
        return isinstance(result, dict) and result.get('contract_version') == 1 and result.get('ready') is True

    def action(self, owner, case_id, revision, action, key, body_hash, request_id):
        # The wrapper owns begin_api_request -> action -> durable job ->
        # complete_api_request, on ONE DB connection/transaction.
        name = 'accept_and_delete_work_case' if action == 'ACCEPT_INCREASE' else 'submit_work_case_start'
        return self.call(name, p_owner=owner, p_case=str(case_id), p_revision=revision,
                         p_key=key, p_hash=body_hash, p_request_id=request_id)

    def context(self, owner, case_id, revision, batch):
        try:
            rows = (self.client.table('work_cases').select('*').eq('owner_user_id', owner)
                    .eq('id', str(case_id)).limit(1).execute().data)
        except Exception:
            raise ServiceError('PROCESSING_UNAVAILABLE', '업무 정보를 확인할 수 없습니다.', 503) from None
        if not rows:
            raise ServiceError('RESOURCE_NOT_FOUND', '업무를 찾을 수 없습니다.', 404)
        row = rows[0]
        if row['input_revision'] != revision or str(row.get('current_batch_id')) != str(batch):
            raise ServiceError('REVISION_CONFLICT', '검토 입력이 변경됐습니다.', 409)
        if row.get('archived_at') or row['status'] != 'Running' or row.get('processing_step') != 'EXTRACTING':
            raise ServiceError('ACTION_NOT_ALLOWED', '검토 실행이 중단됐습니다.', 409)
        return row

    def revision(self, owner, case_id):
        try:
            rows = (self.client.table('work_cases').select('input_revision').eq('owner_user_id', owner)
                    .eq('id', str(case_id)).limit(1).execute().data)
        except Exception:
            raise ServiceError('PROCESSING_UNAVAILABLE', '입력 버전을 확인할 수 없습니다.', 503) from None
        if not rows:
            raise ServiceError('RESOURCE_NOT_FOUND', '업무를 찾을 수 없습니다.', 404)
        return rows[0]['input_revision']

    def mail(self, owner, case_id, mail_id):
        try:
            rows = (self.client.table('mail_messages').select('id,subject,body_text,attachments_jsonb,sender')
                    .eq('owner_user_id', owner).eq('work_case_id', str(case_id))
                    .eq('id', str(mail_id)).limit(1).execute().data)
        except Exception:
            raise ServiceError('PROCESSING_UNAVAILABLE', '메일 근거를 확인할 수 없습니다.', 503) from None
        if not rows:
            raise ServiceError('RESOURCE_NOT_FOUND', '메일 근거를 찾을 수 없습니다.', 404)
        return rows[0]

    def claim(self):
        return self.call('claim_case_extraction')

    def heartbeat(self, job):
        if self.call('heartbeat_case_extraction', p_job=job['job_id'], p_token=job['claim_token']) is not True:
            raise ServiceError('EXTRACTION_LEASE_LOST', '실행 소유권이 변경됐습니다.', 409)

    def finish(self, job, output, result, model_id):
        return self.call('complete_case_extraction', p_job=job['job_id'], p_token=job['claim_token'],
                         p_revision=job['input_revision'], p_batch=job['batch_id'],
                         p_output=output.model_dump(mode='json'),
                         p_result=result.model_dump(mode='json') if result else None,
                         p_model=model_id, p_prompt_version='extraction-v1')

    def fail(self, job, code):
        return self.call('fail_case_extraction', p_job=job['job_id'], p_token=job['claim_token'], p_code=code)

    def recover(self):
        return self.call('recover_case_extractions')
