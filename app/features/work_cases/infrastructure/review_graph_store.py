"""Scoped later-role stores. No direct Gmail send or unguarded card UPDATE."""
from copy import deepcopy
from uuid import UUID
from app.core.errors import ServiceError
from app.features.work_cases.infrastructure.graph_store import GraphStore
from app.features.work_cases.domain.rfq import mailbox_address


class ReviewGraphStore(GraphStore):
    def __init__(self, scope, job):
        super().__init__(scope, job)
        self.role_outputs, self.role_order = {}, []
        self.card_id = None
        self.evidence_ids = tuple(job['evidence_mail_ids'])

    def mails(self, case_id):
        self.check(case_id)
        rows = self.rows(self.repo.client.table('mail_messages').select(
            'id,subject,body_text,sender,attachments_jsonb,supplier_id,message_role,direction,sent_or_received_at')
            .eq('owner_user_id', self.scope.owner).eq('work_case_id', str(case_id))
            .in_('id', list(self.evidence_ids)).limit(501))
        if len(rows) > 500 or any(row['id'] not in self.evidence_ids for row in rows):
            raise ValueError('Review evidence scope mismatch')
        return rows

    def document_row(self, mail_id):
        self.check(self.scope.case_id)
        if str(mail_id) not in self.evidence_ids:
            raise ServiceError('RESOURCE_NOT_FOUND', '메일 원문을 찾을 수 없습니다.', 404)
        rows = self.rows(self.repo.client.table('mail_messages').select('id,attachments_jsonb,normalized_jsonb')
            .eq('owner_user_id', self.scope.owner).eq('work_case_id', self.scope.case_id)
            .eq('id', str(mail_id)).limit(1))
        if not rows:
            raise ServiceError('RESOURCE_NOT_FOUND', '메일 원문을 찾을 수 없습니다.', 404)
        return rows[0]

    def insert_quote_version(self, record, expected_input_revision):
        # RECHECK_SOURCE may cite an earlier mail in this owned case, not only
        # the reply that triggered this execution. The RPC rechecks membership.
        self.check(record.work_case_id)
        self.document_row(record.source_mail_id)
        source = next((mail for mail in self.mails(record.work_case_id) if mail['id'] == record.source_mail_id), None)
        if source is None or source.get('direction') != 'IN' or source.get('supplier_id') != record.supplier_id:
            raise ServiceError('ACTION_NOT_ALLOWED', '견적 원문과 거래처 연결을 확인해야 합니다.', 409)
        if expected_input_revision != self.scope.revision:
            raise ServiceError('REVISION_CONFLICT', '검토 입력이 변경됐습니다.', 409)
        from app.agents.tools import QuoteVersionRecord
        result = self.rpc('store_case_graph_quote', p_record=record.model_dump(mode='json'))
        if isinstance(result, dict) and result.get('kind') == 'STALE':
            return None
        if not isinstance(result, dict) or result.get('kind') != 'STORED':
            raise ValueError('Quote persistence not confirmed')
        saved = QuoteVersionRecord.model_validate(result['record'])
        if (saved.work_case_id != self.scope.case_id or saved.source_mail_id != record.source_mail_id
                or saved.supplier_id != record.supplier_id):
            raise ValueError('Quote identity mismatch')
        qid = str(UUID(result['quote_id']))
        self.records[qid] = saved
        self.saved_ids.add(qid)
        return qid, saved

    def list_case_mails(self, case_id):
        from app.agents.nodes import read_document
        self.check(case_id)
        for item in self.list_unread_attachments(case_id, self.evidence_ids):
            content = self.download_attachment(case_id, item['mail_id'], item['attachment_ref'])
            if content is not None:
                doc = read_document(item['filename'], item.get('content_type'), content)
                self.save_document(case_id, item['mail_id'], item['attachment_ref'], doc)
        return super().list_case_mails(case_id)

    def get_quote_versions(self, case_id, ids):
        self.check(case_id)
        ids = list(dict.fromkeys(str(UUID(value)) for value in ids))
        if len(ids) > 100:
            raise ValueError('Comparison quote bound')
        if not ids:
            return []
        from app.features.work_cases.infrastructure.quote_snapshot import QUOTE_COLUMNS, decode_quote
        rows = self.rows(self.repo.client.table('quote_versions').select(QUOTE_COLUMNS)
            .eq('owner_user_id', self.scope.owner).eq('work_case_id', str(case_id)).in_('id', ids))
        result = []
        for row in rows:
            quote_id, record = decode_quote(row)
            if (row['id'] not in ids or record.work_case_id != str(case_id)
                    or record.supplier_id != row['supplier_id'] or record.source_mail_id != row['source_mail_id']):
                raise ValueError('Comparison quote scope mismatch')
            self.records[quote_id] = record
            result.append((quote_id, record))
        if {key for key, _ in result} != set(ids):
            raise ServiceError('QUOTE_CONTRACT_REQUIRED', '비교 견적의 저장 형식을 확인해야 합니다.', 503)
        return result

    def get_increase_terms(self, case_id, supplier_id):
        case = self.get_work_case(case_id)
        row = self.document_row(case['initial_mail_id'])
        mail = next((mail for mail in self.mails(case_id) if mail['id'] == row['id']), None)
        if mail is None or mail.get('supplier_id') != str(supplier_id):
            return None
        fields = (row.get('normalized_jsonb') or {}).get('fields') or {}
        def value(key):
            item = fields.get(key)
            return item.get('value') if isinstance(item, dict) else None
        return {'mail_id': row['id'], 'previous_unit_price': value('previous_unit_price'),
                'increase_reason': value('increase_reason')}

    def start_agent_run(self, case_id, role, input_revision, input_snapshot, model_id, prompt_version):
        self.check(case_id)
        if str(role) not in {'INFO_EXTRACTOR', 'INFO_PRIORITY', 'RESPONSE_PLAN', 'COUNTER_OFFER'} or input_revision != self.scope.revision:
            raise ServiceError('ACTION_NOT_ALLOWED', '회신 검토 역할 범위를 벗어났습니다.', 409)
        result = self.rpc('begin_case_graph_agent_run', p_role=str(role), p_input=input_snapshot,
                          p_model=model_id, p_prompt_version=prompt_version)
        run_id = str(UUID(result['agent_run_id']))
        if str(result.get('review_batch_id')) != self.scope.batch_id or run_id in self.role_runs:
            raise ValueError('Review role identity mismatch')
        self.role_runs[run_id] = str(role)
        self.role_order.append(run_id)
        return {'agent_run_id': run_id, 'review_batch_id': self.scope.batch_id}

    def finish_agent_run(self, agent_run_id, status, output, error):
        super().finish_agent_run(agent_run_id, status, output, error)
        if status == 'COMPLETED':
            self.role_outputs[str(agent_run_id)] = deepcopy(output)

    def insert_mail_draft(self, record, expected_input_revision):
        from app.agents.tools import MailDraftRecord
        self.check(record.work_case_id)
        if (expected_input_revision != self.scope.revision or record.purpose not in {'CLARIFICATION', 'COUNTER_OFFER'}
                or record.replaces_id is not None):
            raise ServiceError('ACTION_NOT_ALLOWED', '회신 검토 초안 범위를 벗어났습니다.', 409)
        supplier = self.get_supplier(record.work_case_id, record.supplier_id)
        if not supplier or mailbox_address(record.recipient) != mailbox_address(supplier['contact_email']):
            raise ServiceError('ACTION_NOT_ALLOWED', '등록 거래처 수신 주소를 확인해야 합니다.', 409)
        if '\r' in record.subject or '\n' in record.subject:
            raise ValueError('Invalid subject')
        if not self.role_order:
            raise ValueError('Missing review draft role')
        expected_role = 'INFO_PRIORITY' if record.purpose == 'CLARIFICATION' else 'COUNTER_OFFER'
        if self.role_runs[self.role_order[-1]] != expected_role:
            raise ValueError('Review draft purpose/role mismatch')
        result = self.rpc('stage_case_graph_review_draft', p_run=self.role_order[-1], p_record=record.model_dump(mode='json'))
        if isinstance(result, dict) and result.get('kind') == 'STALE':
            return None
        if not isinstance(result, dict) or result.get('kind') != 'STAGED':
            raise ValueError('Review draft staging not confirmed')
        draft_id = str(UUID(result['draft_id']))
        saved = MailDraftRecord.model_validate(result['record'])
        if saved.model_dump(mode='json') != record.model_dump(mode='json'):
            raise ValueError('Changed review draft snapshot')
        self.draft_ids.add(draft_id)
        self.draft_records[draft_id] = saved
        return draft_id, saved

    def save_review_card(self, case_id, expected_input_revision, card):
        from app.agents.schemas import ReviewCard
        from app.agents.tools import revalidate_plans
        self.check(case_id)
        parsed = ReviewCard.model_validate(card)
        if str(parsed.case_id) != str(case_id) or expected_input_revision != self.scope.revision or parsed.input_revision != self.scope.revision:
            raise ValueError('Review card input mismatch')
        latest = set(self.latest_ids(case_id))
        if set(parsed.quote_version_ids) != latest or not parsed.plans:
            raise ValueError('Review card missing current evidence/plans')
        plans = [plan.model_dump(mode='json') for plan in parsed.plans]
        if revalidate_plans(self, case_id, plans)['check'] != 'PASS':
            raise ValueError('Review plans no longer valid')
        final_runs = {}
        for run_id in self.role_order:
            if run_id in self.role_outputs:
                final_runs[self.role_runs[run_id]] = run_id
        if not {'RESPONSE_PLAN', 'COUNTER_OFFER'} <= final_runs.keys():
            raise ValueError('Review completion roles missing')
        plan_output = self.role_outputs[final_runs['RESPONSE_PLAN']]
        counter_output = self.role_outputs[final_runs['COUNTER_OFFER']]
        if plan_output.get('next_action') != 'PLANS_PROPOSED' or plans != plan_output.get('plans'):
            raise ValueError('Card plans differ from completed role output')
        summary = parsed.counter_offer
        expected_status = {'COUNTER_OFFER_PROPOSED': 'PROPOSED', 'COUNTER_OFFER_SKIPPED': 'SKIPPED', 'HOLD': 'HOLD'}
        if summary is None or summary.status != expected_status.get(counter_output.get('next_action')):
            raise ValueError('Counter-offer completion mismatch')
        summary_data = summary.model_dump(mode='json')
        for key in ('target', 'reference', 'draft_ids', 'reason'):
            if summary_data.get(key) != counter_output.get(key, [] if key == 'draft_ids' else None):
                raise ValueError('Card counter-offer differs from completed role output')
        if summary.status != 'HOLD':
            from app.agents.tools import revalidate_counter_offer
            if revalidate_counter_offer(self, case_id, self.scope.revision, summary_data['target'],
                    summary.status == 'SKIPPED', plans)['check'] != 'PASS':
                raise ValueError('Counter-offer validation changed')
        closed = parsed.counter_offer is not None and parsed.counter_offer.status == 'HOLD'
        conditional = any(plan.validation_status != 'VALID' for plan in parsed.plans)
        payload = parsed.model_dump(mode='json')
        payload.update(source_quote_ids=sorted(latest), source_run_ids=list(final_runs.values()),
            review_outcome='REVIEW_REQUIRED_CLOSED' if closed or conditional else 'COMPLETED')
        if closed or conditional:
            payload['closure_reason'] = '역제안은 보류됐으며 구매안 검토를 종료했습니다.' if closed else '확인 조건이 남은 구매안으로 검토를 종료했습니다.'
        # Stage only: committing Complete here would break the graph's final
        # revision check and could ignore unresolved outbound drafts.
        result = self.rpc('stage_case_graph_review_card', p_batch=self.scope.batch_id, p_card=payload)
        if isinstance(result, dict) and result.get('kind') == 'STALE':
            return None
        if not isinstance(result, dict) or result.get('kind') != 'STAGED':
            raise ValueError('Review card staging not confirmed')
        self.card_id = str(UUID(result['card_id']))
        return self.card_id
