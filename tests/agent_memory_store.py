"""06 데이터 스키마 모양의 메모리 저장소. 에이전트 도구 테스트 전용이며 실제 DB 구현이 아니다."""
from app.agents.tools import QuoteVersionRecord


class MemoryStore:
    """CaseStore·QuoteStore·AgentRunStore를 한 객체로 구현한다. 테이블은 06 이름의 dict·list다."""

    def __init__(self):
        self.work_cases, self.mail_messages, self.suppliers = {}, {}, {}
        self.quote_versions: dict[str, QuoteVersionRecord] = {}
        self.agent_runs, self.price_history, self.workflow_logs = {}, [], []
        self.read_failures = 0

    # 테스트 데이터
    def add_case(self, case_id, input_revision=1, item_name=None, specification=None, **fields):
        self.work_cases[case_id] = {'input_revision': input_revision, 'item_name': item_name,
                                    'specification': specification, 'review_batch_id': f'batch-{case_id[:4]}', **fields}

    def add_supplier(self, supplier_id, name):
        self.suppliers[supplier_id] = {'name': name}

    def add_mail(self, mail_id, case_id, body, supplier_id=None, attachments=(), message_role='INITIAL',
                 received_at='2026-10-29T09:00:00+09:00'):
        self.mail_messages[mail_id] = {
            'work_case_id': case_id, 'supplier_id': supplier_id, 'message_role': message_role, 'direction': 'IN',
            'subject': f'{mail_id} 제목', 'received_at': received_at, 'body_text': body,
            'normalized_jsonb': {}, 'attachments': [dict(a) for a in attachments]}

    # CaseStore
    def current_input_revision(self, case_id):
        case = self.work_cases.get(case_id)
        return case and case['input_revision']

    def get_work_case(self, case_id):
        if self.read_failures:
            self.read_failures -= 1
            raise ConnectionError('temporary')
        case = self.work_cases.get(case_id)
        if case is None:
            return None
        latest = {}
        for quote_id, row in self.quote_versions.items():
            if row.work_case_id == case_id and row.version_no >= latest.get(row.supplier_id, (0, ''))[0]:
                latest[row.supplier_id] = (row.version_no, quote_id)
        return {**case, 'latest_quote_version_ids': [quote_id for _, quote_id in latest.values()]}

    def list_case_mails(self, case_id):
        return [{'mail_id': mail_id, **{k: row[k] for k in ('message_role', 'direction', 'supplier_id', 'subject')},
                 'received_at': row['received_at'],
                 'attachments': [{'attachment_ref': a['attachment_ref'], 'filename': a['filename'],
                                  'pages': len(a['pages'])} for a in row['attachments']]}
                for mail_id, row in self.mail_messages.items() if row['work_case_id'] == case_id]

    def read_mail_part(self, case_id, mail_id, part, page):
        mail = self.mail_messages.get(mail_id)
        if mail is None or mail['work_case_id'] != case_id:
            return None
        if part == 'body':
            return {'text': mail['body_text'], 'location': f'{mail_id} 본문'}
        attachment = next((a for a in mail['attachments'] if a['attachment_ref'] == part), None)
        index = (page or 1) - 1
        if attachment is None or not 0 <= index < len(attachment['pages']):
            return None
        return {'text': attachment['pages'][index], 'location': f'{mail_id} {attachment["filename"]} p.{index + 1}'}

    def save_agent_output(self, case_id, agent_run_id, role, expected_input_revision, output):
        run = self.agent_runs.get(agent_run_id)
        if run is None or run['work_case_id'] != case_id or run['agent_role'] != role:
            return 'RUN_MISMATCH'
        if self.current_input_revision(case_id) != expected_input_revision:
            return 'REVISION_CHANGED'
        run['output_jsonb'] |= output
        return 'SAVED'

    # QuoteStore
    def resolve_supplier(self, case_id, supplier_name):
        matches = [sid for sid, row in self.suppliers.items() if row['name'] == supplier_name]
        return matches[0] if len(matches) == 1 else None

    def latest_quote_version(self, case_id, supplier_id):
        rows = [(qid, row) for qid, row in self.quote_versions.items()
                if row.work_case_id == case_id and row.supplier_id == supplier_id]
        return max(rows, key=lambda item: item[1].version_no) if rows else None

    def insert_quote_version(self, record, expected_input_revision):
        case = self.work_cases.get(record.work_case_id)
        if case is None or case['input_revision'] != expected_input_revision:
            return None
        same = {'version_no', 'supersedes_id', 'case_conditions'}
        content = record.model_dump(exclude=same)
        for qid, row in self.quote_versions.items():
            if row.model_dump(exclude=same) == content:
                return qid, row
        if any(row.work_case_id == record.work_case_id and row.supplier_id == record.supplier_id
               and row.version_no == record.version_no for row in self.quote_versions.values()):
            raise ValueError('UNIQUE(work_case_id, supplier_id, version_no)')
        quote_id = f'qv-{len(self.quote_versions) + 1}'
        self.quote_versions[quote_id] = record
        if record.unit_price is not None:
            self.price_history.append({'quote_version_id': quote_id, 'unit_price': record.unit_price,
                                       'unit': record.unit, 'effective_from': record.effective_from})
        for mail_id, normalized in record.mail_normalized.items():
            stored = self.mail_messages[mail_id]['normalized_jsonb']
            notes = stored.get('item_notes', [])
            stored |= {'schema_version': normalized['schema_version'],
                       'fields': stored.get('fields', {}) | normalized['fields'],
                       'item_notes': notes + [n for n in normalized['item_notes'] if n not in notes]}
        if record.case_conditions:
            for key, value in record.case_conditions.items():
                if case.get(key) is None:
                    case[key] = value
        self.workflow_logs.append({'event_type': 'TOOL_RESULT', 'quote_version_id': quote_id})
        return quote_id, record

    # AgentRunStore
    def start_agent_run(self, case_id, role, input_revision, input_snapshot, model_id, prompt_version):
        run_id = f'run-{len(self.agent_runs) + 1}'
        batch = self.work_cases[case_id]['review_batch_id']
        self.agent_runs[run_id] = {
            'work_case_id': case_id, 'review_batch_id': batch, 'component_type': 'AGENT', 'agent_role': role,
            'input_revision': input_revision, 'input_snapshot_jsonb': input_snapshot, 'output_jsonb': {},
            'status': 'RUNNING', 'model_id': model_id, 'prompt_version': prompt_version, 'error_jsonb': None}
        return {'agent_run_id': run_id, 'review_batch_id': batch}

    def finish_agent_run(self, agent_run_id, status, output, error):
        run = self.agent_runs[agent_run_id]
        run['status'], run['error_jsonb'] = status, error
        if output:
            run['output_jsonb'] |= output


from copy import deepcopy
from threading import RLock
from app.agents.tools import OutputValidationError, ExecutionScope, calculation_digest

class SyntheticCalculationStore:
    """Explicit EVAL_ONLY in-memory test repository, NOT a real DB adapter."""

    def __init__(self, snapshots, authorized_case_ids):
        self._lock = RLock()
        self._cases = deepcopy(snapshots)
        self._records = {}
        self.authorized = frozenset(map(str, authorized_case_ids))
        self.audit = []
        for value in self._cases.values():
            if value.get('usage') != 'EVAL_ONLY' or value['data'].get('data_kind') != 'SYNTHETIC':
                raise ValueError('Synthetic repository accepts explicitly labelled evaluation data only')
            value['data'].pop('schema_version', None)
            value['data'].pop('contract_revision', None)
            value['data']['item'].pop('item_id', None)
            for quote in value['data']['quotes']:quote['item'].pop('item_id', None)

    def _case(self, case):
        if case not in self.authorized or case not in self._cases:
            raise OutputValidationError('CASE_SCOPE_DENIED')
        return self._cases[case]

    def current_revision(self, case_id):
        with self._lock:
            return self._case(str(case_id))['revision']

    def scope_for_case(self, case_id, revision):
        """Resolve authenticated current quote IDs, never IDs supplied by a draft."""
        with self._lock:
            case = self._case(str(case_id))
            scope = ExecutionScope(str(case_id), revision, tuple(sorted(case['quote_bindings'])))
            self._check(scope)
            return scope

    def _check(self, scope):
        case = self._case(scope.case_id)
        if type(scope.revision) is not int or scope.revision != case['revision']:
            raise OutputValidationError('STALE_REVISION')
        if scope.quote_ids != tuple(sorted(case['quote_bindings'])):
            raise OutputValidationError('STALE_QUOTE_VERSIONS')
        return case

    def snapshot(self, scope):
        with self._lock:
            return deepcopy(self._check(scope))

    def check_scope(self, scope):
        with self._lock:
            self._check(scope)

    def resolve_draft(self, scope, ref):
        """Synthetic draft storage only; not the team's shared draft service."""
        return self.resolve(scope, 'DRAFT', ref)

    def put_if_current(self, scope, kind, payload):
        with self._lock:
            case = self._check(scope)
            record = {'case_id': scope.case_id, 'input_revision': scope.revision, 'quote_version_ids': list(scope.quote_ids), 'snapshot_digest': calculation_digest(case), 'kind': kind, 'payload': deepcopy(payload)}
            ref = 'EVAL_ONLY:' + kind + ':' + calculation_digest(record)
            self._records[ref] = record
            self.audit.append({'action': 'write', 'kind': kind, 'ref': ref, 'revision': scope.revision})
            return ref

    def resolve(self, scope, kind, ref):
        with self._lock:
            case = self._check(scope)
            record = self._records.get(ref)
            if record is None:
                raise OutputValidationError('REFERENCE_NOT_FOUND')
            if record['case_id'] != scope.case_id or record['input_revision'] != scope.revision or record['quote_version_ids'] != list(scope.quote_ids) or (record['kind'] != kind):
                raise OutputValidationError('REFERENCE_SCOPE_MISMATCH')
            if record['snapshot_digest'] != calculation_digest(case):
                raise OutputValidationError('REFERENCE_SOURCE_CHANGED')
            if ref != 'EVAL_ONLY:' + kind + ':' + calculation_digest(record):
                raise OutputValidationError('REFERENCE_TAMPERED')
            return deepcopy(record['payload'])

    def advance_revision(self, case_id):
        """Evaluation-only new evidence event. Existing records become unreadable."""
        with self._lock:
            case = self._case(str(case_id))
            case['revision'] += 1
            case['data']['revision'] = case['revision']


class RoleMemoryStore:
    """Test-only implementations of the typed role Store protocols; no DB/client."""
    def __init__(self, common, facts=None, references=None):
        self.common, self.facts, self.references = common, facts, references
        self.priority_records = {}
        self.priority_lock = RLock()

    def _input(self, context, role):
        from app.agents.schemas import AGENT_IO
        row = self.common.agent_runs.get(context.agent_run_id)
        if not row or row['work_case_id'] != context.case_id or row['agent_role'] != role:
            raise OutputValidationError('TRUSTED_AGENT_INPUT_REQUIRED')
        if row['input_revision'] != context.input_revision:
            raise OutputValidationError('STALE_REVISION')
        return AGENT_IO[role][0].model_validate(deepcopy(row['input_snapshot_jsonb']))

    def load_priority_input(self, context):
        from app.agents.schemas import AgentRole
        return self._input(context, AgentRole.INFO_PRIORITY)

    def load_plan_input(self, context):
        from app.agents.schemas import AgentRole
        return self._input(context, AgentRole.RESPONSE_PLAN)

    def load_counter_offer_input(self, context):
        from app.agents.schemas import AgentRole
        return self._input(context, AgentRole.COUNTER_OFFER)

    def load_review_facts(self, context):
        from app.agents.schemas import ReviewFactsRecord
        if self.facts is None:return None
        scope = ExecutionScope(context.case_id, context.input_revision,
            tuple(sorted(self.common.get_work_case(context.case_id)['latest_quote_version_ids'])))
        record = self.facts.load(scope)
        converted = {'quotes': {}}
        for key, value in record['facts'].items():
            # Convert the existing regression fixture to the new typed store model.
            # Retired prototype IDs/eligibility/policy copies are not production inputs.
            if key == 'policy_source' or key.endswith('.eligibility'):continue
            value = deepcopy(value)
            if key.endswith('item_identity'):value['value'].pop('item_id', None)
            if '.' in key:
                qid, name = key.split('.', 1)
                converted['quotes'].setdefault(qid, {})[name] = value
            else:converted[key] = value
        return ReviewFactsRecord.model_validate({**record, 'facts': converted})

    def _save(self, name, kind, context, scope, source_digest, record):
        from app.agents.tools import MissingReviewData
        if self.references is None:raise MissingReviewData([name])
        return self.references.put_if_current(scope, source_digest, kind,
            record.model_dump(mode='json', by_alias=True, exclude_unset=True), context.agent_run_id)

    def _read(self, name, kind, model, context, scope, source_digest, ref):
        from app.agents.tools import MissingReviewData
        if self.references is None:raise MissingReviewData([name])
        return model.model_validate(self.references.resolve(scope, source_digest, kind, ref))

    def save_plan_set(self, *args):return self._save('PlanStore.save_plan_set', 'PLAN_SET', *args)
    def save_plan_calculation(self, *args):return self._save('PlanStore.save_plan_calculation', 'PLAN_CALCULATION', *args)
    def save_price_calculation(self, *args):return self._save('CounterOfferStore.save_price_calculation', 'OFFER_CALCULATION', *args)
    def save_price_validation(self, *args):return self._save('CounterOfferStore.save_price_validation', 'PRICE_VALIDATION', *args)
    def save_policy(self, *args):return self._save('CounterOfferStore.save_policy', 'POLICY', *args)

    def read_plan_set(self, *args):
        from app.agents.schemas import PlanSetRecord
        return self._read('PlanStore.read_plan_set', 'PLAN_SET', PlanSetRecord, *args)

    def read_plan_calculation(self, *args):
        from app.agents.schemas import PlanCalculationRecord
        return self._read('PlanStore.read_plan_calculation', 'PLAN_CALCULATION', PlanCalculationRecord, *args)

    def read_price_calculation(self, *args):
        from app.agents.schemas import PriceCalculationRecord
        return self._read('CounterOfferStore.read_price_calculation', 'OFFER_CALCULATION', PriceCalculationRecord, *args)

    def read_price_validation(self, *args):
        from app.agents.schemas import PriceValidationRecord
        return self._read('CounterOfferStore.read_price_validation', 'PRICE_VALIDATION', PriceValidationRecord, *args)

    def read_policy(self, *args):
        from app.agents.schemas import PolicyRecord
        return self._read('CounterOfferStore.read_policy', 'POLICY', PolicyRecord, *args)


    def save_break_even(self, context, scope, source_digest, record):
        from app.agents import tools
        from app.agents.schemas import AgentRole
        with self.priority_lock:
            if self.references and self.references.before_write:self.references.before_write()
            original = self.load_priority_input(context)
            if ExecutionScope.from_input(original) != scope:raise OutputValidationError('TOOL_SCOPE_QUOTE_MISMATCH')
            repo = tools._ReviewAccess(context, AgentRole.INFO_PRIORITY)
            result = tools.compute_break_even(repo, scope, record.alternative_quote_version_id, record.question_quote_version_id)
            if result['status'] != 'CALCULATED' or result['source_digest'] != source_digest:
                raise OutputValidationError('REFERENCE_SOURCE_CHANGED')
            if tools.break_even_record(repo, scope, record.alternative_quote_version_id, result) != record:
                raise OutputValidationError('BREAK_EVEN_REFERENCE_MISMATCH')
            row={'scope':vars(scope),'record':record.model_dump(mode='json',by_alias=True)}
            ref='SYNTHETIC-break-even:' + calculation_digest(row)
            self.priority_records[ref]=deepcopy(row)
            return ref

    def read_break_even(self, context, scope, ref):
        from app.agents.schemas import BreakEvenRecord
        if self.common.current_input_revision(context.case_id) != context.input_revision:
            raise OutputValidationError('STALE_REVISION')
        row=self.priority_records.get(ref)
        if row is None or row['scope'] != vars(scope) or ref != 'SYNTHETIC-break-even:' + calculation_digest(row):
            raise OutputValidationError('REFERENCE_SCOPE_MISMATCH')
        return BreakEvenRecord.model_validate(deepcopy(row['record']))
