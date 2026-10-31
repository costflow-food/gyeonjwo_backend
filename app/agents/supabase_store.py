"""06 데이터 스키마(Supabase gyeonjwo-dev) 저장소. 에이전트 도구의 CaseStore·QuoteStore·CandidateStore·MailDraftStore와
AgentRunner의 AgentRunStore를 한 객체로 구현한다. 소유 범위는 업무의 owner_user_id로 제한한다.

supabase-py는 여러 쿼리를 한 트랜잭션으로 묶지 못한다. 저장은 revision 확인 → 중복 확인 → 순서대로 쓰기로 하고,
같은 내용을 다시 저장하면 기존 행을 돌려주므로 중간 실패 뒤 재실행해도 행이 늘지 않는다.
"""
import uuid
from datetime import datetime, timezone

from supabase import Client

from app.agents.tools import MailDraftRecord, QuoteVersionRecord

POLICY_VERSION = 'agents-v1'
# mail_drafts.intent_id: 연결할 테이블이 아직 없어 업무·revision·목적에서 고정 uuid를 만든다 (팀 확인 전 임시)
_INTENT_NS = uuid.UUID('5d7c6a8e-3f0b-4c1e-9a52-1f0e6b7d2c40')
_QUOTE_SAME = {'version_no', 'supersedes_id', 'case_conditions', 'mail_normalized'}
_QUOTE_COLUMNS = ('work_case_id', 'supplier_id', 'source_mail_id', 'version_no', 'supersedes_id', 'unit_price', 'unit',
                  'shipping_cost', 'available_quantity', 'effective_from', 'valid_until', 'checks_jsonb',
                  'pricing_terms_jsonb', 'cost_terms_jsonb', 'item_notes_jsonb', 'evidence_jsonb', 'extraction_status',
                  'currency', 'moq_quantity', 'moq_unit', 'order_increment_quantity', 'order_increment_unit')


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _str(value):
    return None if value is None else str(value)


class SupabaseStore:
    def __init__(self, client: Client):
        self.db = client
        self._owners: dict[str, str] = {}

    # 공통
    def _rows(self, table, columns='*', **eq):
        query = self.db.table(table).select(columns)
        for key, value in eq.items():
            query = query.eq(key, value)
        return query.execute().data

    def _case(self, case_id, columns='*'):
        rows = self._rows('work_cases', columns, id=case_id)
        return rows[0] if rows else None

    def _owner(self, case_id):
        if case_id not in self._owners:
            case = self._case(case_id, 'owner_user_id')
            if case is None:
                return None
            self._owners[case_id] = case['owner_user_id']
        return self._owners[case_id]

    def _log(self, case_id, event_type, actor_id, payload, input_revision=None, agent_run_id=None, mail_id=None):
        self.db.table('workflow_logs').insert({
            'owner_user_id': self._owner(case_id), 'work_case_id': case_id, 'agent_run_id': agent_run_id,
            'source_mail_id': mail_id, 'event_type': event_type, 'actor_type': 'AGENT', 'actor_id': actor_id,
            'input_revision': input_revision, 'policy_version': POLICY_VERSION, 'payload_jsonb': payload}).execute()

    # CaseStore
    def current_input_revision(self, case_id):
        case = self._case(case_id, 'input_revision')
        return case and case['input_revision']

    def get_work_case(self, case_id):
        case = self._case(case_id)
        if case is None:
            return None
        latest = {}
        for row in self._rows('quote_versions', 'id,supplier_id,version_no', work_case_id=case_id):
            if row['version_no'] >= latest.get(row['supplier_id'], (0, ''))[0]:
                latest[row['supplier_id']] = (row['version_no'], row['id'])
        return {**case, 'mandatory_conditions': case['mandatory_conditions_jsonb'], 'policy': case['policy_jsonb'],
                'quantity': _str(case['quantity']), 'latest_quote_version_ids': [qid for _, qid in latest.values()]}

    @staticmethod
    def _documents(mail):
        """첨부 페이지 텍스트: normalized_jsonb.documents[{attachment_ref, filename, pages:[text]}] (문서 읽기·OCR 결과)."""
        return {doc['attachment_ref']: doc for doc in (mail.get('normalized_jsonb') or {}).get('documents', [])}

    def list_case_mails(self, case_id):
        mails = []
        for row in self._rows('mail_messages', work_case_id=case_id):
            documents = self._documents(row)
            attachments = [{'attachment_ref': a['attachment_ref'], 'filename': a.get('filename'),
                            'pages': len(documents.get(a['attachment_ref'], {}).get('pages', []))}
                           for a in row['attachments_jsonb'] or [] if a.get('attachment_ref')]
            mails.append({'mail_id': row['id'], 'message_role': row['message_role'], 'direction': row['direction'],
                          'supplier_id': row['supplier_id'], 'subject': row['subject'],
                          'received_at': row['sent_or_received_at'], 'attachments': attachments})
        return mails

    def read_mail_part(self, case_id, mail_id, part, page):
        rows = self._rows('mail_messages', 'id,work_case_id,body_text,normalized_jsonb', id=mail_id)
        if not rows or rows[0]['work_case_id'] != case_id:
            return None
        mail = rows[0]
        if part == 'body':
            return {'text': mail['body_text'], 'location': f'{mail_id} 본문'}
        document = self._documents(mail).get(part)
        index = (page or 1) - 1
        if document is None or not 0 <= index < len(document.get('pages', [])):
            return None
        return {'text': document['pages'][index], 'location': f'{mail_id} {document.get("filename")} p.{index + 1}'}

    def save_agent_output(self, case_id, agent_run_id, role, expected_input_revision, output):
        rows = self._rows('agent_runs', 'work_case_id,agent_role,output_jsonb', id=agent_run_id)
        if not rows or rows[0]['work_case_id'] != case_id or rows[0]['agent_role'] != role:
            return 'RUN_MISMATCH'
        if self.current_input_revision(case_id) != expected_input_revision:
            return 'REVISION_CHANGED'
        merged = (rows[0]['output_jsonb'] or {}) | output
        self.db.table('agent_runs').update({'output_jsonb': merged}).eq('id', agent_run_id).execute()
        return 'SAVED'

    # QuoteStore
    def resolve_supplier(self, case_id, supplier_name):
        matches = self._rows('suppliers', 'id', owner_user_id=self._owner(case_id), name=supplier_name)
        return matches[0]['id'] if len(matches) == 1 else None

    @staticmethod
    def _record(row):
        return QuoteVersionRecord(**{key: row[key] for key in _QUOTE_COLUMNS},
                                  mail_normalized={}, case_conditions=None)

    def latest_quote_version(self, case_id, supplier_id):
        rows = self._rows('quote_versions', work_case_id=case_id, supplier_id=supplier_id)
        if not rows:
            return None
        row = max(rows, key=lambda r: r['version_no'])
        return row['id'], self._record(row)

    def _supplier_item_id(self, case, supplier_id):
        if not case.get('standardized_item_key'):
            return None
        rows = self._rows('supplier_items', 'id', supplier_id=supplier_id,
                          standardized_item_key=case['standardized_item_key'])
        return rows[0]['id'] if len(rows) == 1 else None

    def insert_quote_version(self, record, expected_input_revision):
        case = self._case(record.work_case_id)
        if case is None or case['input_revision'] != expected_input_revision:
            return None
        content = record.model_dump(exclude=_QUOTE_SAME)
        for row in self._rows('quote_versions', work_case_id=record.work_case_id, supplier_id=record.supplier_id):
            existing = self._record(row)
            if existing.model_dump(exclude=_QUOTE_SAME) == content:
                return row['id'], existing
        owner = case['owner_user_id']
        supplier_item_id = self._supplier_item_id(case, record.supplier_id)
        values = {key: getattr(record, key) for key in _QUOTE_COLUMNS}
        quote = self.db.table('quote_versions').insert({
            **values, 'owner_user_id': owner, 'supplier_item_id': supplier_item_id,
            'input_revision': expected_input_revision}).execute().data[0]
        mail = self._rows('mail_messages', 'sent_or_received_at', id=record.source_mail_id)
        if record.unit_price is not None and record.unit and supplier_item_id and mail:
            self.db.table('price_history').insert({
                'owner_user_id': owner, 'supplier_item_id': supplier_item_id, 'quote_version_id': quote['id'],
                'source_mail_id': record.source_mail_id, 'unit_price': record.unit_price, 'unit': record.unit,
                'currency': record.currency, 'received_at': mail[0]['sent_or_received_at'],
                'effective_from': record.effective_from, 'valid_until': record.valid_until,
                'conditions_jsonb': record.pricing_terms_jsonb, 'evidence_jsonb': record.evidence_jsonb}).execute()
        for mail_id, normalized in record.mail_normalized.items():
            stored = self._rows('mail_messages', 'normalized_jsonb', id=mail_id)[0]['normalized_jsonb'] or {}
            notes = stored.get('item_notes', [])
            stored |= {'schema_version': normalized['schema_version'],
                       'fields': stored.get('fields', {}) | normalized['fields'],
                       'item_notes': notes + [n for n in normalized['item_notes'] if n not in notes]}
            self.db.table('mail_messages').update({'normalized_jsonb': stored}).eq('id', mail_id).execute()
        if record.case_conditions:
            fill = {key: value for key, value in record.case_conditions.items() if case.get(key) is None}
            if fill:
                self.db.table('work_cases').update({**fill, 'updated_at': _now()}).eq('id', case['id']).execute()
        self._log(case['id'], 'TOOL_RESULT', 'INFO_EXTRACTOR', {'quote_version_id': quote['id']},
                  expected_input_revision, mail_id=record.source_mail_id)
        return quote['id'], self._record(quote)

    # CandidateStore
    def get_candidate_conditions(self, case_id):
        case = self.get_work_case(case_id)
        if case is None:
            return None
        return {key: case.get(key) for key in ('input_revision', 'item_name', 'standardized_item_key', 'specification',
                                               'mandatory_conditions', 'quantity', 'unit', 'due_date', 'policy')}

    def incumbent_supplier_ids(self, case_id):
        """이 업무의 견적 업체와, 업무를 연 메일의 발신 업체."""
        ids = {row['supplier_id'] for row in self._rows('quote_versions', 'supplier_id', work_case_id=case_id)}
        case = self._case(case_id, 'initial_mail_id')
        if case:
            mail = self._rows('mail_messages', 'supplier_id', id=case['initial_mail_id'])
            ids |= {row['supplier_id'] for row in mail if row['supplier_id']}
        return ids

    def list_supplier_items(self, case_id, item_key):
        rows = self._rows('supplier_items', owner_user_id=self._owner(case_id), standardized_item_key=item_key)
        for row in rows:
            # NOT NULL 컬럼이라 미입력도 []로 저장된다. 비어 있으면 "인증 없음"이 아니라 미확인으로 넘긴다
            row['certifications_jsonb'] = row['certifications_jsonb'] or None
        return rows

    def get_supplier(self, case_id, supplier_id):
        rows = self._rows('suppliers', owner_user_id=self._owner(case_id), id=supplier_id)
        return rows[0] if rows else None

    def get_quote_price(self, case_id, quote_version_id):
        rows = self._rows('quote_versions', 'unit_price,unit,currency', owner_user_id=self._owner(case_id),
                          id=quote_version_id)
        return rows and {'unit_price': _str(rows[0]['unit_price']), 'unit': rows[0]['unit']}

    def list_purchase_history(self, case_id, supplier_item_ids):
        if not supplier_item_ids:
            return []
        rows = (self.db.table('purchase_history').select('*').eq('owner_user_id', self._owner(case_id))
                .in_('supplier_item_id', supplier_item_ids).execute().data)
        return [{**row, 'unit_price': _str(row['unit_price']), 'quantity': _str(row['quantity'])} for row in rows]

    # MailDraftStore
    def _send_settings(self, case_id):
        case = self._case(case_id, 'mailbox_ref,owner_user_id')
        if case is None:
            return None
        rows = (self.db.table('mail_send_settings').select('*').eq('owner_user_id', case['owner_user_id'])
                .eq('mailbox_ref', case['mailbox_ref']).order('version_no', desc=True).limit(1).execute().data)
        return rows[0] if rows else None

    def current_send_settings(self, case_id):
        settings = self._send_settings(case_id)
        return dict(settings['style_jsonb']) if settings else {}

    def insert_mail_draft(self, record, expected_input_revision):
        if self.current_input_revision(record.work_case_id) != expected_input_revision:
            return None
        existing = self._rows('mail_drafts', idempotency_key=record.idempotency_key)
        if existing:
            row = existing[0]
            return row['id'], MailDraftRecord(**{key: row[key] for key in MailDraftRecord.model_fields})
        intent = uuid.uuid5(_INTENT_NS, f'{record.work_case_id}|{record.input_revision}|{record.purpose}')
        settings = self._send_settings(record.work_case_id)
        row = self.db.table('mail_drafts').insert({
            **record.model_dump(), 'owner_user_id': self._owner(record.work_case_id), 'intent_id': str(intent),
            'send_settings_id': settings and settings['id']}).execute().data[0]
        return row['id'], record

    # AgentRunStore
    def start_agent_run(self, case_id, role, input_revision, input_snapshot, model_id, prompt_version):
        case = self._case(case_id, 'owner_user_id,current_batch_id')
        row = self.db.table('agent_runs').insert({
            'owner_user_id': case['owner_user_id'], 'work_case_id': case_id,
            'review_batch_id': case['current_batch_id'], 'component_type': 'AGENT', 'agent_role': role,
            'input_revision': input_revision, 'input_snapshot_jsonb': input_snapshot, 'output_jsonb': {},
            'status': 'RUNNING', 'model_id': model_id, 'prompt_version': prompt_version,
            'policy_version': POLICY_VERSION, 'started_at': _now()}).execute().data[0]
        return {'agent_run_id': row['id'], 'review_batch_id': case['current_batch_id']}

    def finish_agent_run(self, agent_run_id, status, output, error):
        update = {'status': status, 'error_jsonb': error, 'finished_at': _now()}
        if output:
            stored = self._rows('agent_runs', 'output_jsonb', id=agent_run_id)[0]['output_jsonb'] or {}
            update['output_jsonb'] = stored | output
        self.db.table('agent_runs').update(update).eq('id', agent_run_id).execute()
