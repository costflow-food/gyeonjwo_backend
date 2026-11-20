"""평가용 메모리 저장소. app.agents.tools의 저장소 Protocol(CaseStore·QuoteStore·CandidateStore·MailDraftStore·
ComparisonStore·PriorityStore·CounterOfferStore)을 한 클래스로 구현한다.

DB 대신 시나리오 데이터를 들고 있고, 도구가 부른 저장소 메서드를 calls에 기록한다(채점에서 검색 횟수·조회 범위 확인).
시나리오 하나에 저장소 하나를 만든다. 다른 시나리오와 공유하지 않는다.
"""
from __future__ import annotations

import copy
import itertools

from app.agents.tools import MailDraftRecord, QuoteVersionRecord

CASE_ID = '00000000-0000-4000-8000-000000000001'


class MemoryStore:
    def __init__(self, case: dict, suppliers: list[dict], items: list[dict], purchases: list[dict] | None = None,
                 mails: list[dict] | None = None, style: dict | None = None):
        self.case = {'id': CASE_ID, 'input_revision': 1, 'policy': {}, 'standardized_item_key': None} | case
        self.suppliers = {row['id']: row for row in suppliers}
        self.items = list(items)
        self.purchases = list(purchases or [])
        self.mails = {mail['mail_id']: mail for mail in mails or []}
        self.style = style or {}
        self.quotes: dict[str, QuoteVersionRecord] = {}
        self.drafts: dict[str, MailDraftRecord] = {}
        self.issues: list[dict] = []
        self.calls: list[tuple[str, dict]] = []
        self._ids = itertools.count(1)

    # ── 기록 ──
    def _log(self, name: str, **args):
        self.calls.append((name, args))

    def calls_of(self, name: str) -> list[dict]:
        return [args for called, args in self.calls if called == name]

    # ── CaseStore ──
    def current_input_revision(self, case_id):
        return self.case['input_revision'] if case_id == CASE_ID else None

    def latest_ids(self) -> list[str]:
        latest = {}
        for quote_id, record in self.quotes.items():      # 넣은 순서대로라 마지막이 최신
            latest[record.supplier_id] = quote_id
        return list(latest.values())

    def get_work_case(self, case_id):
        if case_id != CASE_ID:
            return None
        return copy.deepcopy(self.case) | {'latest_quote_version_ids': self.latest_ids()}

    def list_case_mails(self, case_id):
        if case_id != CASE_ID:
            return []
        keys = ('mail_id', 'channel', 'message_role', 'direction', 'supplier_id', 'received_at', 'subject')
        return [{key: mail.get(key) for key in keys} | {
            'attachments': [{key: a.get(key) for key in ('attachment_ref', 'filename', 'pages', 'status')}
                            for a in mail.get('attachments') or []]} for mail in self.mails.values()]

    def read_mail_part(self, case_id, mail_id, part, page):
        self._log('read_mail_part', mail_id=mail_id, part=part, page=page)
        mail = self.mails.get(mail_id) if case_id == CASE_ID else None
        if mail is None:
            return None
        if part == 'body':
            return {'text': mail['body'], 'location': 'body'}
        for attachment in mail.get('attachments') or []:
            if attachment['attachment_ref'] == part:
                pages = attachment.get('texts') or []
                index = (page or 1) - 1
                if 0 <= index < len(pages):
                    return {'text': pages[index], 'location': f'{part} p{index + 1}'}
        return None

    # ── QuoteStore ──
    def resolve_supplier(self, case_id, supplier_name):
        found = [sid for sid, row in self.suppliers.items() if row.get('name') == supplier_name]
        return found[0] if len(found) == 1 else None

    def latest_quote_version(self, case_id, supplier_id):
        found = [(qid, record) for qid, record in self.quotes.items() if record.supplier_id == supplier_id]
        return found[-1] if found else None

    def list_supplier_items(self, case_id, supplier_id):
        return [{key: item.get(key) for key in ('id', 'item_name', 'specification', 'unit', 'standardized_item_key')}
                for item in self.items if item['supplier_id'] == supplier_id]

    def record_extraction_issue(self, case_id, agent_run_id, issue):
        self.issues.append(issue)

    def insert_quote_version(self, record, expected_input_revision):
        if expected_input_revision != self.case['input_revision']:
            return None
        for name, value in (record.case_conditions or {}).items():   # 비어 있는 업무 조건만 채운다
            if self.case.get(name) in (None, ''):
                self.case[name] = value
        quote_id = f'q-{next(self._ids)}'
        self.quotes[quote_id] = record
        return quote_id, record

    # ── CandidateStore ──
    def get_candidate_conditions(self, case_id):
        case = self.get_work_case(case_id)
        if case is None:
            return None
        keys = ('id', 'input_revision', 'item_name', 'specification', 'mandatory_conditions', 'quantity', 'unit',
                'due_date', 'policy', 'standardized_item_key')
        return {key: case.get(key) for key in keys}

    def incumbent_supplier_ids(self, case_id):
        return {record.supplier_id for record in self.quotes.values()}

    def search_supplier_items(self, case_id, keywords, limit):
        self._log('search_supplier_items', keywords=list(keywords), limit=limit)
        rows = [item for item in self.items
                if any(word.casefold() in (item.get('item_name') or '').casefold() for word in keywords)]
        return [self._item_row(item) for item in rows[:limit]]

    @staticmethod
    def _item_row(item):
        keys = ('id', 'supplier_id', 'item_name', 'specification', 'unit', 'certifications_jsonb', 'origin', 'notes',
                'last_purchase_id', 'standardized_item_key')
        return {key: copy.deepcopy(item.get(key)) for key in keys}

    def get_supplier_items(self, case_id, ids):
        self._log('get_supplier_items', ids=list(ids))
        wanted = set(ids)
        return [self._item_row(item) for item in self.items if item['id'] in wanted]

    def get_supplier(self, case_id, supplier_id):
        row = self.suppliers.get(supplier_id)
        return copy.deepcopy(row) if row else None

    def list_purchase_history(self, case_id, supplier_item_ids):
        wanted = set(supplier_item_ids)
        return [dict(row) for row in self.purchases if row['supplier_item_id'] in wanted]

    # ── MailDraftStore ──
    def current_send_settings(self, case_id):
        return dict(self.style)

    def insert_mail_draft(self, record, expected_input_revision):
        if expected_input_revision != self.case['input_revision']:
            return None
        for draft_id, row in self.drafts.items():
            if row.idempotency_key == record.idempotency_key:
                return draft_id, row
        draft_id = f'd-{next(self._ids)}'
        self.drafts[draft_id] = record
        return draft_id, record

    # ── ComparisonStore ──
    def get_quote_versions(self, case_id, ids):
        return [(qid, self.quotes[qid]) for qid in ids if qid in self.quotes]

    # ── CounterOfferStore ──
    def get_increase_terms(self, case_id, supplier_id):
        """그 업체 메일의 normalized_jsonb.fields 중 previous_unit_price·increase_reason (서버 RPC와 같은 뜻)."""
        found = {}
        for record in self.quotes.values():
            if record.supplier_id != supplier_id:
                continue
            for mail_id, normalized in (record.mail_normalized or {}).items():
                fields = normalized.get('fields') or {}
                for name in ('previous_unit_price', 'increase_reason'):
                    if name in fields and fields[name].get('value') is not None:
                        found[name] = fields[name]['value']
                        found['mail_id'] = mail_id
        if 'previous_unit_price' not in found and 'increase_reason' not in found:
            return None
        return {'previous_unit_price': found.get('previous_unit_price'),
                'increase_reason': found.get('increase_reason'), 'mail_id': found.get('mail_id')}
