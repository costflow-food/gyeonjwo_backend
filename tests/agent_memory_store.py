"""06 데이터 스키마 모양의 메모리 저장소. 에이전트 평가·테스트 전용이며 실제 DB 구현이 아니다.

CaseStore·QuoteStore(①)·CandidateStore·MailDraftStore(②)·AgentRunStore를 한 객체로 구현한다.
2026-11-02 현재 app/agents 코드(dev_1.0 b3f8a56) 기준으로 다시 작성했다.
"""
from app.agents.tools import MailDraftRecord, QuoteVersionRecord


class MemoryStore:
    def __init__(self):
        self.work_cases, self.mail_messages, self.suppliers = {}, {}, {}
        self.supplier_items, self.purchase_history = {}, {}
        self.quote_versions: dict[str, QuoteVersionRecord] = {}
        self.mail_drafts: dict[str, MailDraftRecord] = {}
        self.incumbents: dict[str, set[str]] = {}
        self.send_settings: dict = {}
        self.agent_runs, self.price_history, self.workflow_logs = {}, [], []
        self.extraction_issues: list[dict] = []

    # ── 테스트 데이터 ──
    def add_case(self, case_id, input_revision=1, item_name=None, specification=None, **fields):
        self.work_cases[case_id] = {'input_revision': input_revision, 'item_name': item_name,
                                    'specification': specification, 'review_batch_id': f'batch-{case_id[-4:]}',
                                    'mandatory_conditions': None, 'quantity': None, 'unit': None, 'due_date': None,
                                    'policy': {}, **fields}

    def add_supplier(self, supplier_id, name, contact_email=None, evidence=None, source_type='REGISTERED'):
        self.suppliers[supplier_id] = {'id': supplier_id, 'name': name, 'contact_email': contact_email,
                                       'evidence_jsonb': evidence, 'source_type': source_type}

    def add_supplier_item(self, item_id, supplier_id, item_name, specification=None, certifications=None,
                          origin=None, notes=None, unit='kg'):
        self.supplier_items[item_id] = {'id': item_id, 'supplier_id': supplier_id, 'item_name': item_name,
                                        'specification': specification, 'unit': unit,
                                        'certifications_jsonb': certifications, 'origin': origin, 'notes': notes,
                                        'last_purchase_id': None}

    def add_purchase(self, purchase_id, supplier_item_id, unit_price, unit, transaction_date, quantity='100',
                     currency='KRW'):
        item = self.supplier_items[supplier_item_id]
        self.purchase_history[purchase_id] = {
            'id': purchase_id, 'supplier_item_id': supplier_item_id, 'supplier_id': item['supplier_id'],
            'transaction_date': transaction_date, 'quantity': quantity, 'unit': unit, 'unit_price': unit_price,
            'total_amount': None, 'currency': currency}
        item['last_purchase_id'] = purchase_id

    def add_incumbent(self, case_id, supplier_id):
        self.incumbents.setdefault(case_id, set()).add(supplier_id)

    def add_mail(self, mail_id, case_id, body, supplier_id=None, attachments=(), message_role='INITIAL',
                 received_at='2026-10-29T09:00:00+09:00', subject=None):
        self.mail_messages[mail_id] = {
            'work_case_id': case_id, 'supplier_id': supplier_id, 'message_role': message_role, 'direction': 'IN',
            'subject': subject or f'{mail_id} 제목', 'received_at': received_at, 'body_text': body,
            'normalized_jsonb': {}, 'attachments': [dict(a) for a in attachments]}

    def source_texts(self, case_id) -> list[str]:
        """업무의 모든 원문(제목·본문·첨부 쪽). 평가에서 '원문에 없는 값'을 찾을 때 쓴다."""
        texts = []
        for row in self.mail_messages.values():
            if row['work_case_id'] == case_id:
                texts += [row['subject'], row['body_text']] + [p for a in row['attachments'] for p in a['pages']]
        return texts

    # ── CaseStore ──
    def current_input_revision(self, case_id):
        case = self.work_cases.get(case_id)
        return case and case['input_revision']

    def get_work_case(self, case_id):
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
                                  'pages': len(a['pages']), 'status': 'READ' if any(p.strip() for p in a['pages'])
                                  else 'NO_TEXT'} for a in row['attachments']]}
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

    # ── QuoteStore (①) ──
    def resolve_supplier(self, case_id, supplier_name):
        matches = [sid for sid, row in self.suppliers.items() if row['name'] == supplier_name]
        return matches[0] if len(matches) == 1 else None

    def list_supplier_items(self, case_id, supplier_id):
        return [{key: row.get(key) for key in ('id', 'item_name', 'specification', 'unit', 'standardized_item_key')}
                for row in self.supplier_items.values() if row['supplier_id'] == supplier_id]

    def record_extraction_issue(self, case_id, agent_run_id, issue):
        self.extraction_issues.append({'case_id': case_id, 'agent_run_id': agent_run_id, **issue})

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
        quote_id = f'qv-{len(self.quote_versions) + 1}'
        self.quote_versions[quote_id] = record
        if record.unit_price is not None:
            self.price_history.append({'quote_version_id': quote_id, 'unit_price': record.unit_price,
                                       'unit': record.unit, 'effective_from': record.effective_from})
        for mail_id, normalized in record.mail_normalized.items():
            stored = self.mail_messages[mail_id]['normalized_jsonb']
            notes = stored.get('item_notes', [])
            stored |= {'schema_version': normalized.get('schema_version'),
                       'fields': stored.get('fields', {}) | normalized.get('fields', {}),
                       'item_notes': notes + [n for n in normalized.get('item_notes', []) if n not in notes]}
        if record.case_conditions:
            for key, value in record.case_conditions.items():
                if case.get(key) is None:
                    case[key] = value
        self.add_incumbent(record.work_case_id, record.supplier_id)
        return quote_id, record

    # ── ComparisonStore (③④⑤) ──
    def get_quote_versions(self, case_id, ids):
        return [(qid, self.quote_versions[qid]) for qid in ids
                if qid in self.quote_versions and self.quote_versions[qid].work_case_id == case_id]

    # ── CounterOfferStore (⑤) ──
    def get_increase_terms(self, case_id, supplier_id):
        for mail_id, row in self.mail_messages.items():
            if row['work_case_id'] != case_id or row['supplier_id'] != supplier_id:
                continue
            fields = (row['normalized_jsonb'] or {}).get('fields', {})
            value = lambda name: (fields.get(name) or {}).get('value')  # noqa: E731
            if value('previous_unit_price') is not None or value('increase_reason') is not None:
                return {'previous_unit_price': value('previous_unit_price'),
                        'increase_reason': value('increase_reason'), 'mail_id': mail_id}
        return None

    # ── CandidateStore (②) ──
    def get_candidate_conditions(self, case_id):
        case = self.work_cases.get(case_id)
        if case is None:
            return None
        return {key: case.get(key) for key in ('input_revision', 'item_name', 'specification', 'mandatory_conditions',
                                               'quantity', 'unit', 'due_date', 'policy')}

    def incumbent_supplier_ids(self, case_id):
        return set(self.incumbents.get(case_id, set()))

    def search_supplier_items(self, case_id, keywords, limit):
        rows = [row for row in self.supplier_items.values()
                if any(word.casefold() in row['item_name'].casefold() for word in keywords)]
        return [dict(row) for row in rows[:limit]]

    def get_supplier_items(self, case_id, ids):
        return [dict(self.supplier_items[i]) for i in ids if i in self.supplier_items]

    def get_supplier(self, case_id, supplier_id):
        row = self.suppliers.get(supplier_id)
        return dict(row) if row else None

    def list_purchase_history(self, case_id, supplier_item_ids):
        return [dict(row) for row in self.purchase_history.values() if row['supplier_item_id'] in supplier_item_ids]

    # ── MailDraftStore (②) ──
    def current_send_settings(self, case_id):
        return dict(self.send_settings)

    def insert_mail_draft(self, record, expected_input_revision):
        if self.current_input_revision(record.work_case_id) != expected_input_revision:
            return None
        for draft_id, row in self.mail_drafts.items():
            if row.idempotency_key == record.idempotency_key:
                return draft_id, row
        draft_id = f'draft-{len(self.mail_drafts) + 1}'
        self.mail_drafts[draft_id] = record
        return draft_id, record

    # ── AgentRunStore ──
    def start_agent_run(self, case_id, role, input_revision, input_snapshot, model_id, prompt_version):
        run_id = f'run-{len(self.agent_runs) + 1}'
        batch = self.work_cases[case_id]['review_batch_id']
        self.agent_runs[run_id] = {
            'work_case_id': case_id, 'review_batch_id': batch, 'agent_role': str(role), 'input_revision': input_revision,
            'output_jsonb': {}, 'status': 'RUNNING', 'model_id': model_id, 'prompt_version': prompt_version,
            'error_jsonb': None}
        return {'agent_run_id': run_id, 'review_batch_id': batch}

    def finish_agent_run(self, agent_run_id, status, output, error):
        run = self.agent_runs[agent_run_id]
        run['status'], run['error_jsonb'] = status, error
        if output:
            run['output_jsonb'] |= output
