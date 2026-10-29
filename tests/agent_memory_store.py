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
