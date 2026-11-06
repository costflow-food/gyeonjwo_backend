"""One mutation RPC per transaction. No multi-request REST pseudo-transactions.

RPCs in integration_contracts/extraction_runtime.json must be implemented by the
DB owner before enabling this feature. This adapter never installs SQL.
"""
from threading import Lock
from time import monotonic

from app.core.errors import ServiceError


class CaseRuntimeRepository:
    def __init__(self, client):
        self.client = client
        self._schema_lock = Lock()
        self._schema_cache = None

    def call(self, name, **params):
        try:
            result = self.client.rpc(name, params).execute().data
        except Exception as error:
            if getattr(error, 'code', None) == 'PGRST202':
                raise ServiceError('EXTRACTION_SCHEMA_REQUIRED', 'Supabase의 업무 실행·저장 함수 구성을 확인해야 합니다.', 503,
                                   retryable=False, next_action='CONTACT_SERVER_ADMIN') from None
            raise ServiceError('PROCESSING_UNAVAILABLE', '업무 실행 저장소를 확인할 수 없습니다.', 503) from None
        if isinstance(result, dict) and result.get('kind') == 'ERROR':
            codes = {'RESOURCE_NOT_FOUND': 404, 'WORK_ALREADY_STARTED': 409, 'REVISION_CONFLICT': 409,
                     'ACTION_NOT_ALLOWED': 409, 'REQUEST_IN_PROGRESS': 409, 'IDEMPOTENCY_KEY_REUSED': 409,
                     'INVALID_PLAN_SELECTION': 422, 'DRAFT_CHANGED': 409, 'SEND_RESULT_UNKNOWN': 409,
                     'INVALID_CONDITIONS': 422}
            code = result.get('code')
            if code in codes:
                raise ServiceError(code, '최신 업무 상태와 요청을 확인해 주세요.', codes[code],
                                   current_revision=result.get('current_revision'), retryable=False)
            raise ServiceError('PROCESSING_UNAVAILABLE', '업무 변경을 확인할 수 없습니다.', 503)
        return result

    def case_change_receipt(self, owner, case_id, operation, key, body_hash):
        return self.call('lookup_work_case_change_request', p_owner=str(owner), p_case=str(case_id),
                         p_operation=operation, p_key=key, p_hash=body_hash)

    def related_case_ids(self, owner, case_id, initial_mail_id):
        """Read siblings by canonical initial mail, never by a shared reply."""
        related = []
        after = None
        try:
            while True:
                query = (self.client.table('work_cases').select('id')
                         .eq('owner_user_id', str(owner)).eq('initial_mail_id', str(initial_mail_id))
                         .neq('id', str(case_id)).order('id').limit(100))
                if after is not None:
                    query = query.gt('id', after)
                rows = query.execute().data
                if not isinstance(rows, list):
                    raise ValueError('Invalid related cases')
                ids = [row['id'] for row in rows]
                if ids != sorted(set(ids)) or (after is not None and ids and ids[0] <= after):
                    raise ValueError('Invalid related case order')
                related.extend(ids)
                if len(ids) < 100:
                    return related
                after = ids[-1]
        except Exception:
            raise ServiceError('PROCESSING_UNAVAILABLE', '관련 업무를 조회할 수 없습니다.', 503) from None

    def submit_case_change(self, owner, case_id, operation, body, key, body_hash, request_id):
        return self.call('submit_work_case_change', p_owner=str(owner), p_case=str(case_id),
                         p_operation=operation, p_body=body, p_key=key, p_hash=body_hash,
                         p_request_id=request_id)

    def available(self):
        try:
            result = self.call('review_start_health')
        except ServiceError as error:
            if error.code != 'EXTRACTION_SCHEMA_REQUIRED':
                raise
            result = self.call('case_agent_store_health')
        return isinstance(result, dict) and result.get('contract_version') == 1 and result.get('ready') is True

    def action(self, owner, case_id, revision, action, key, body_hash, request_id):
        # The wrapper owns begin_api_request -> action -> durable job ->
        # complete_api_request, on ONE DB connection/transaction.
        names = {'ACCEPT_INCREASE': 'accept_and_delete_work_case', 'START': 'submit_work_case_start'}
        if action not in names:
            raise ServiceError('ACTION_NOT_ALLOWED', '허용하지 않은 업무 동작입니다.', 409, retryable=False)
        name = names[action]
        return self.call(name, p_owner=owner, p_case=str(case_id), p_revision=revision,
                         p_key=key, p_hash=body_hash, p_request_id=request_id)

    def resume_available(self):
        """Inspect published signatures only; never probe a mutation by calling it."""
        expected = {
            'submit_work_case_resume': {'p_owner', 'p_case', 'p_revision', 'p_reason', 'p_key', 'p_hash', 'p_request_id'},
            'lookup_work_case_resume_request': {'p_owner', 'p_case', 'p_key', 'p_hash'},
        }
        return self.rpc_signatures_available(expected)

    def case_changes_available(self):
        return self.rpc_signatures_available({
            'lookup_work_case_change_request': {'p_owner', 'p_case', 'p_operation', 'p_key', 'p_hash'},
            'submit_work_case_change': {'p_owner', 'p_case', 'p_operation', 'p_body', 'p_key', 'p_hash', 'p_request_id'},
        })

    def rpc_signatures_available(self, expected):
        try:
            with self._schema_lock:
                if self._schema_cache is None or monotonic() >= self._schema_cache[0]:
                    try:
                        response = self.client.postgrest.session.get('', headers={'Accept': 'application/openapi+json'})
                        response.raise_for_status()
                        schema = response.json()
                        paths = schema['paths']
                        if not isinstance(paths, dict):
                            raise ValueError('Invalid OpenAPI paths')
                    except Exception:
                        self._schema_cache = (monotonic() + 5, None)
                    else:
                        self._schema_cache = (monotonic() + 120, schema)
                schema = self._schema_cache[1]
            if schema is None:
                raise ValueError('OpenAPI unavailable')
            paths = schema['paths']
            for name, args in expected.items():
                parameters = paths.get('/rpc/' + name, {}).get('post', {}).get('parameters', [])
                body = next((p.get('schema', {}) for p in parameters if p.get('in') == 'body'), {})
                if set(body.get('properties', {})) != args or set(body.get('required', [])) != args:
                    return False
            return True
        except Exception:
            raise ServiceError('PROCESSING_UNAVAILABLE', '재개 저장소의 연결 상태를 확인할 수 없습니다.', 503) from None

    def cached_column_missing(self, table, column):
        cached = self._schema_cache
        if cached is None or cached[1] is None or monotonic() >= cached[0]:
            return False
        schema = cached[1]
        properties = (schema.get('definitions') or {}).get(table, {}).get('properties')
        return isinstance(properties, dict) and bool(properties) and column not in properties

    def resume_receipt(self, owner, case_id, revision, reason, key):
        from app.features.work_cases.application.actions import resume_body_hash
        try:
            result = self.call('lookup_work_case_resume_request', p_owner=str(owner), p_case=str(case_id),
                               p_key=key, p_hash=resume_body_hash(revision, reason))
        except ServiceError as error:
            if error.code == 'EXTRACTION_SCHEMA_REQUIRED':
                raise ServiceError('RESUME_INTEGRATION_REQUIRED', '보류 업무의 재개 저장 기능이 아직 연결되지 않았습니다.',
                                   503, retryable=False, next_action='CONTACT_SERVER_ADMIN') from None
            raise
        if not isinstance(result, dict) or result.get('kind') not in {'NONE', 'REPLAY'}:
            raise ServiceError('PROCESSING_UNAVAILABLE', '재개 접수 이력을 확인할 수 없습니다.', 503)
        return result if result['kind'] == 'REPLAY' else None

    def resume(self, owner, case_id, revision, reason, key, body_hash, request_id):
        return self.call('submit_work_case_resume', p_owner=owner, p_case=str(case_id), p_revision=revision,
                         p_reason=reason, p_key=key, p_hash=body_hash, p_request_id=request_id)

    def context(self, owner, case_id, revision, batch):
        try:
            rows = (self.client.table('work_cases').select(
                    'id,input_revision,current_batch_id,status,processing_step,archived_at,'
                    'item_name,specification,quantity::text,unit,due_date,mandatory_conditions_jsonb,policy_jsonb').eq('owner_user_id', owner)
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

    def fail(self, job, code):
        return self.call('fail_case_extraction', p_job=job['job_id'], p_token=job['claim_token'], p_code=code)

    def recover(self):
        return self.call('recover_case_extractions')

    def finish_agent(self, job, output, quote_ids, metadata):
        return self.call('complete_case_agent_extraction', p_job=job['job_id'], p_token=job['claim_token'],
            p_revision=job['input_revision'], p_batch=job['batch_id'],
            p_output=output.model_dump(mode='json'), p_quote_ids=quote_ids,
            p_model=metadata.get('model_id') or 'unavailable',
            p_prompt_version=metadata.get('prompt_version') or 'agent-owned',
            p_agent_output=metadata.get('agent_output'))

    def start_receipt(self, owner, case_id, revision, key, *, conditions=None):
        from app.features.work_cases.application.actions import revision_body_hash
        try:
            result = self.call('lookup_work_case_start_request', p_owner=str(owner), p_case=str(case_id),
                               p_key=key, p_hash=revision_body_hash(revision, conditions))
        except ServiceError as error:
            # Optional existing Supabase lookup. Absence requires no schema
            # installation: the transactional submit RPC still owns replay.
            if error.code == 'EXTRACTION_SCHEMA_REQUIRED':
                return None
            raise
        if not isinstance(result, dict) or result.get('kind') not in {'NONE', 'REPLAY'}:
            raise ServiceError('PROCESSING_UNAVAILABLE', '검토 접수 이력을 확인할 수 없습니다.', 503)
        return result if result['kind'] == 'REPLAY' else None
