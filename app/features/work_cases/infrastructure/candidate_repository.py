"""Immutable candidate sets in agent_runs; selection audit in workflow_logs."""
from uuid import UUID
from app.shared.infrastructure.db.health_probe import HealthProbe
from app.core.errors import ServiceError
from app.features.work_cases.domain.candidates import CandidateSnapshot, CandidateSelectionResponse, POLICY_MODE
from app.features.work_cases.application.start_readiness import StartReadiness

CASE_COLUMNS = 'id,status,processing_step,input_revision,current_batch_id,archived_at,decision,mailbox_ref'
RUN_COLUMNS = 'id,owner_user_id,work_case_id,input_revision,review_batch_id,output_jsonb,finished_at'


class CandidateRepository:
    def __init__(self, client, *, readiness_probe=None, graph_health=None):
        self.client = client
        self.graph_health = graph_health
        self.readiness_probe = readiness_probe or HealthProbe(self._check_readiness, lambda result: result.available)

    def rows(self, query):
        try:
            rows = query.execute().data
            if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
                raise ValueError('Invalid rows')
            return rows
        except Exception:
            raise ServiceError('PROCESSING_UNAVAILABLE', '후보 정보를 조회할 수 없습니다.', 503) from None

    def case(self, owner, case_id):
        rows = self.rows(self.client.table('work_cases').select(CASE_COLUMNS)
                         .eq('owner_user_id', str(owner)).eq('id', str(case_id)).limit(1))
        if not rows:
            raise ServiceError('RESOURCE_NOT_FOUND', '업무를 찾을 수 없습니다.', 404)
        return rows[0]

    def snapshot(self, row, owner):
        try:
            snapshot = CandidateSnapshot.model_validate(row['output_jsonb']['candidate_set'])
            if (str(row['owner_user_id']) != str(owner) or str(snapshot.candidate_set_id) != str(row['id'])
                    or str(snapshot.case_id) != str(row['work_case_id'])
                    or str(snapshot.review_batch_id) != str(row['review_batch_id'])
                    or snapshot.revision != row['input_revision']):
                raise ValueError('Invalid set scope')
            return snapshot
        except Exception:
            raise ServiceError('PROCESSING_UNAVAILABLE', '저장된 후보 세트 계약을 확인할 수 없습니다.', 503) from None

    def run_query(self, owner):
        return (self.client.table('agent_runs').select(RUN_COLUMNS).eq('owner_user_id', str(owner))
                .eq('component_type', 'AGENT').eq('agent_role', 'CANDIDATE').eq('status', 'COMPLETED')
                .order('finished_at', desc=True).order('id', desc=True))

    def latest(self, owner, case_id):
        rows = self.rows(self.run_query(owner).eq('work_case_id', str(case_id))
                         .not_.is_('output_jsonb->candidate_set', 'null').limit(1))
        if not rows:
            raise ServiceError('CANDIDATES_NOT_READY', '후보 선정 결과가 아직 준비되지 않았습니다.', 404)
        if str(rows[0].get('work_case_id')) != str(case_id):
            raise ServiceError('PROCESSING_UNAVAILABLE', '후보 세트의 업무 범위를 확인할 수 없습니다.', 503)
        return self.snapshot(rows[0], owner)

    def for_cases(self, owner, cases):
        if len(cases) > 40:
            result = {}
            for start in range(0, len(cases), 40):
                result.update(self.for_cases(owner, cases[start:start + 40]))
            return result
        cases = {str(case['id']): case for case in cases if case.get('current_batch_id')}
        if not cases:
            return {}
        rows = self.rows(self.run_query(owner).in_('work_case_id', list(cases))
                         .in_('review_batch_id', sorted({str(c['current_batch_id']) for c in cases.values()}))
                         .not_.is_('output_jsonb->candidate_set', 'null').limit(1000))
        if len(rows) >= 1000:
            raise ServiceError('PROCESSING_UNAVAILABLE', '후보 세트 조회 범위를 확인해야 합니다.', 503)
        result = {}
        for row in rows:
            key = str(row['work_case_id'])
            case = cases.get(key)
            if (case is not None and key not in result and str(row['review_batch_id']) == str(case['current_batch_id'])
                    and row['input_revision'] == case['input_revision']):
                result[key] = self.snapshot(row, owner)
        return result

    def selection(self, owner, case_id, candidate_set_id):
        rows = self.rows(self.client.table('workflow_logs').select('payload_jsonb')
            .eq('owner_user_id', str(owner)).eq('work_case_id', str(case_id)).eq('event_type', 'CANDIDATE_SELECTION')
            .contains('payload_jsonb', {'candidate_set_id': str(candidate_set_id)})
            .order('created_at', desc=True).order('id', desc=True).limit(1))
        if not rows:
            return None
        try:
            selected = CandidateSelectionResponse.model_validate(rows[0]['payload_jsonb'])
            if str(selected.case_id) != str(case_id) or str(selected.candidate_set_id) != str(candidate_set_id):
                raise ValueError('Wrong selection scope')
            return selected
        except Exception:
            raise ServiceError('PROCESSING_UNAVAILABLE', '후보 선택 이력을 확인할 수 없습니다.', 503) from None

    def _check_readiness(self):
        try:
            result = self.client.rpc('candidate_selection_health', {}).execute().data
            if (not isinstance(result, dict) or type(result.get('contract_version')) is not int
                    or result.get('contract_version') != 1 or result.get('ready') is not True
                    or result.get('policy_mode') != POLICY_MODE
                    or result.get('user_selection_gate') is not True or result.get('send_claim_guard') is not True
                    or result.get('unselected_drafts_closed') is not True):
                raise ValueError('Incomplete selection integration')
            return StartReadiness()
        except Exception:
            return StartReadiness('CANDIDATE_SELECTION_INTEGRATION_REQUIRED', '후보 선택·발송 저장 기능이 아직 연결되지 않았습니다.')

    def ready(self):
        return self.listing_readiness(timeout=None)

    def listing_readiness(self, timeout=0.025):
        if self.graph_health is not None:
            # The graph RPC fingerprints candidate_selection_health itself.
            # Its audited body derives readiness solely from graph ready.
            result = self.graph_health(timeout=timeout)
            if isinstance(result, dict) and result.get('ready') is True:
                return StartReadiness()
            return StartReadiness('CANDIDATE_SELECTION_INTEGRATION_REQUIRED',
                                  '후보 선택·발송 저장 기능이 아직 연결되지 않았습니다.')
        return self.readiness_probe.read(timeout=timeout) or StartReadiness(
            'CANDIDATE_SELECTION_INTEGRATION_REQUIRED', '후보 선택 준비 상태를 확인 중입니다.')

    def blocks(self, owner, cases):
        from app.features.work_cases.application.candidates import candidate_block
        readiness = self.listing_readiness()
        if not readiness.available:
            return {str(case['id']): (readiness.code, readiness.message) for case in cases}
        try:
            snapshots = self.for_cases(owner, cases)
            selected = set()
            if snapshots:
                rows = self.rows(self.client.table('workflow_logs').select('work_case_id,payload_jsonb')
                    .eq('owner_user_id', str(owner)).eq('event_type', 'CANDIDATE_SELECTION')
                    .in_('work_case_id', list(snapshots))
                    .in_('payload_jsonb->>candidate_set_id', [str(value.candidate_set_id) for value in snapshots.values()])
                    .limit(len(snapshots) + 1))
                if len(rows) > len(snapshots):
                    raise ServiceError('PROCESSING_UNAVAILABLE', '후보 선택 이력 범위를 확인해야 합니다.', 503)
                for row in rows:
                    record = CandidateSelectionResponse.model_validate(row['payload_jsonb'])
                    key = str(row['work_case_id'])
                    if (key in snapshots and str(record.case_id) == key
                            and record.candidate_set_id == snapshots[key].candidate_set_id):
                        selected.add(key)
            return {str(case['id']): (('ACTION_NOT_ALLOWED', '이미 선택한 요청 대상입니다. 초안·발송 상태를 확인해 주세요.')
                    if str(case['id']) in selected else candidate_block(case, snapshots.get(str(case['id'])), readiness)) for case in cases}
        except Exception:
            return {str(case['id']): ('PROCESSING_UNAVAILABLE', '후보 선택 가능 여부를 확인할 수 없습니다.') for case in cases}

    def call(self, name, params):
        try:
            result = self.client.rpc(name, params).execute().data
        except Exception as error:
            if getattr(error, 'code', None) == 'PGRST202':
                raise ServiceError('CANDIDATE_SELECTION_INTEGRATION_REQUIRED', '후보 선택 저장 함수를 확인해야 합니다.',
                                   503, retryable=False, next_action='CONTACT_SERVER_ADMIN') from None
            raise ServiceError('PROCESSING_UNAVAILABLE', '후보 선택 요청을 처리할 수 없습니다.', 503) from None
        if isinstance(result, dict) and result.get('kind') == 'ERROR':
            code = result.get('code')
            statuses = {'RESOURCE_NOT_FOUND': 404, 'REVISION_CONFLICT': 409, 'CANDIDATES_CHANGED': 409,
                'ACTION_NOT_ALLOWED': 409, 'REQUEST_IN_PROGRESS': 409, 'IDEMPOTENCY_KEY_REUSED': 409,
                'SEND_RESULT_UNKNOWN': 409, 'INVALID_CANDIDATE_SELECTION': 422, 'SEND_SCOPE_REQUIRED': 422,
                'PROCESSING_UNAVAILABLE': 503}
            if code in statuses:
                raise ServiceError(code, '최신 후보·업무·발송 설정을 확인해 주세요.', statuses[code])
            raise ServiceError('PROCESSING_UNAVAILABLE', '후보 선택 결과를 확인할 수 없습니다.', 503)
        return result

    def receipt(self, identity, case_id):
        result = self.call('lookup_case_candidate_selection_request', {'p_owner': identity.user_id,
            'p_case': str(case_id), 'p_key': identity.key, 'p_hash': identity.body_hash})
        if isinstance(result, dict) and result.get('kind') in {'NONE', 'REPLAY'}:
            return result if result['kind'] == 'REPLAY' else None
        raise ServiceError('PROCESSING_UNAVAILABLE', '후보 선택 접수 이력을 확인할 수 없습니다.', 503)

    def submit(self, identity, case_id, body, dispatch_ready, request_id):
        return self.call('submit_case_candidate_selection', {'p_owner': identity.user_id, 'p_case': str(case_id),
            'p_body': body, 'p_key': identity.key, 'p_hash': identity.body_hash, 'p_request_id': request_id,
            'p_dispatch_ready': bool(dispatch_ready), 'p_policy_mode': POLICY_MODE})
