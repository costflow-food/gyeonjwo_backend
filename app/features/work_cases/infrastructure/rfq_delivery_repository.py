"""Owner-scoped drafts; SQL v3.6 owns all send state/approval transitions."""
from uuid import UUID, uuid5, NAMESPACE_URL
from datetime import datetime, timezone
from app.core.errors import ServiceError
from app.features.work_cases.domain.rfq import AuthorizedMail
from app.features.work_cases.infrastructure.rfq_repository import RfqRepository


class RfqDeliveryRepository(RfqRepository):
    def graph_owns_case(self, owner, case_id, batch_id):
        if not batch_id:
            return False
        rows = self.data(self.client.table('agent_runs').select('id')
            .eq('owner_user_id', str(owner)).eq('work_case_id', str(case_id))
            .eq('review_batch_id', str(batch_id))
            .contains('input_snapshot_jsonb', {'backend_execution_mode': 'GRAPH_START_V1'}).limit(1))
        return bool(rows)

    def data(self, query):
        try:
            result = query.execute().data
            if not isinstance(result, list) or any(not isinstance(row, dict) for row in result):
                raise ValueError('Invalid RFQ response')
            return result
        except Exception:
            raise ServiceError('PROCESSING_UNAVAILABLE', 'RFQ 저장소 응답을 확인할 수 없습니다.', 503) from None

    def candidates(self, owner, case):
        key = case.get('standardized_item_key')
        if not key:
            return []
        rows = self.data(self.client.table('supplier_items').select(
            'id,supplier_id,standardized_item_key,specification,certifications_jsonb,origin')
            .eq('owner_user_id', str(owner)).eq('standardized_item_key', key).order('id').limit(101))
        if len(rows) > 100:
            raise ServiceError('PROCESSING_UNAVAILABLE', '후보 검색 범위를 좁혀야 합니다.', 503)
        if not rows:
            return []
        suppliers = self.data(self.client.table('suppliers').select('id,contact_email')
            .eq('owner_user_id', str(owner)).in_('source_type', ['REGISTERED', 'VERIFIED_WEB'])
            .in_('id', sorted({row['supplier_id'] for row in rows})))
        contacts = {row['id']: row.get('contact_email') for row in suppliers}
        from app.features.work_cases.domain.rfq import mailbox_address
        selected = []
        for row in rows:
            try:
                contact = mailbox_address(contacts.get(row['supplier_id']))
            except ValueError:
                continue
            # A known mismatch cannot be overridden by a preference score/model.
            if (case.get('specification') and row.get('specification')
                    and str(case['specification']).strip().casefold() != str(row['specification']).strip().casefold()):
                continue
            selected.append({**row, 'contact_email': contact})
        return selected

    def send_policy(self, owner, case, recipient, purpose='RFQ', supplier_id=None):
        now = datetime.now(timezone.utc).isoformat()
        base_query = lambda: self.client.table('mail_send_settings').select(
            'id,configured,version_no,approval_required_by_purpose,allowed_recipients_jsonb,allowed_scope_jsonb,style_jsonb')\
            .eq('owner_user_id', str(owner)).eq('mailbox_ref', case['mailbox_ref']).lte('effective_at', now)
        case_rows = self.data(base_query().eq('work_case_id', case['id']).order('effective_at', desc=True)
                              .order('version_no', desc=True).order('id', desc=True).limit(1))
        base_rows = self.data(base_query().is_('work_case_id', 'null').order('effective_at', desc=True)
                              .order('version_no', desc=True).order('id', desc=True).limit(1))
        override = case_rows[0] if case_rows else None
        # Null is inheritance, never an explicit OFF or an override of the base.
        chosen = (override if override and type(override['approval_required_by_purpose'].get(purpose)) is bool
                  else (base_rows[0] if base_rows else None))
        if not chosen:
            return {'configured': False, 'auto_eligible': False}
        approval = chosen['approval_required_by_purpose'].get(purpose)
        scope = chosen['allowed_scope_jsonb']
        configured = (chosen['configured'] is True and type(approval) is bool
            and recipient in chosen['allowed_recipients_jsonb']
            and purpose in scope.get('purposes', []) and case['id'] in scope.get('work_case_ids', [])
            and ('supplier_ids' not in scope or (isinstance(scope['supplier_ids'], list)
                and supplier_id is not None and str(supplier_id) in scope['supplier_ids'])))
        return {'configured': configured, 'approval_required': approval,
                'auto_eligible': configured and approval is False,
                'style': chosen.get('style_jsonb') or {},
                'settings_version': (base_rows[0]['id'] if base_rows else 'none') + ':'
                                    + (override['id'] if override else 'none')}

    def begin_candidates(self, owner, case, model_id):
        run_id = str(uuid5(NAMESPACE_URL, 'gyeonjwo:candidate:' + str(owner) + ':' + case['id'] + ':'
                           + str(case['current_batch_id']) + ':' + str(case['input_revision'])))
        try:
            self.client.table('agent_runs').insert(dict(id=run_id, owner_user_id=str(owner),
                work_case_id=case['id'], review_batch_id=case['current_batch_id'], component_type='AGENT',
                agent_role='CANDIDATE', input_revision=case['input_revision'],
                input_snapshot_jsonb={'case_id': case['id'], 'input_revision': case['input_revision']},
                model_id=model_id, prompt_version='candidate-rfq-v1', policy_version='rfq-sql-v3.6')).execute()
        except Exception as error:
            if getattr(error, 'code', None) == '23505':
                raise ServiceError('WORK_ALREADY_STARTED', '후보 선정이 이미 접수되었습니다.', 409) from None
            raise ServiceError('PROCESSING_UNAVAILABLE', '후보 선정 접수를 확인할 수 없습니다.', 503) from None
        return run_id

    def end_candidates(self, owner, run_id, status, output):
        if status not in {'COMPLETED', 'FAILED', 'STALE'}:
            raise ValueError('Invalid candidate run state')
        try:
            rows = self.data(self.client.table('agent_runs').update({'status': status, 'output_jsonb': output,
                'finished_at': datetime.now(timezone.utc).isoformat()})
                .eq('owner_user_id', str(owner)).eq('id', run_id).eq('status', 'RUNNING'))
        except ServiceError:
            persisted = self.candidate_run(owner, run_id)
            if persisted['status'] == status and persisted.get('output_jsonb') == output:
                return
            raise
        if len(rows) != 1:
            persisted = self.candidate_run(owner, run_id)
            if persisted['status'] != status or persisted.get('output_jsonb') != output:
                raise ServiceError('REVISION_CONFLICT', '후보 선정 실행이 이미 종료됐습니다.', 409)

    def draft(self, owner, draft_id):
        try:
            rows = (self.client.table('mail_drafts').select(
                'id,work_case_id,supplier_id,intent_id,purpose,recipient,subject,body_text,input_revision,'
                'status,approved_by,approved_at,style_snapshot_jsonb,settings_snapshot_jsonb,'
                'created_at,replaces_id,sent_provider_message_id,sent_at')
                .eq('owner_user_id', str(owner)).eq('id', str(draft_id)).limit(1).execute().data)
            if not isinstance(rows, list) or len(rows) > 1:
                raise ValueError('Invalid draft response')
        except Exception:
            raise ServiceError('PROCESSING_UNAVAILABLE', '메일 초안을 조회할 수 없습니다.', 503) from None
        if not rows:
            raise ServiceError('RESOURCE_NOT_FOUND', '메일 초안을 찾을 수 없습니다.', 404)
        return rows[0]

    def list_drafts(self, owner, case_id, snapshot, after, limit):
        query = (self.client.table('mail_drafts').select('*').eq('owner_user_id', str(owner))
            .eq('work_case_id', str(case_id)).lte('created_at', snapshot)
            .order('created_at', desc=True).order('id', desc=True).limit(limit))
        visible = ('and(or(style_snapshot_jsonb->>producer.is.null,and(style_snapshot_jsonb->>producer.neq.graph-staged-rfq-v1,style_snapshot_jsonb->>producer.neq.graph-staged-review-v1)),'
            'or(style_snapshot_jsonb->>candidate_selection_required.is.null,style_snapshot_jsonb->>candidate_selection_required.neq.true,'
            'and(style_snapshot_jsonb->>candidate_selection_required.eq.true,style_snapshot_jsonb->candidate_selection->>state.eq.SELECTED)))')
        if after is not None:
            boundary = f'created_at.lt.{after[0]},and(created_at.eq.{after[0]},id.lt.{after[1]})'
            query = query.or_(f'and({visible},or({boundary}))')
        else:
            query = query.or_(visible)
        return self.data(query)

    def submit_draft_action(self, owner, draft_id, action, payload, identity, request_id, accepting):
        return self.call('submit_rfq_draft_action', p_owner=str(owner), p_draft=str(draft_id), p_action=action,
            p_revision=payload.expected_revision if action == 'APPROVE_SEND' else None,
            p_snapshot=payload.snapshot_hash if action == 'APPROVE_SEND' else None,
            p_settings_version=payload.expected_settings_version if action == 'APPROVE_SEND' else None,
            p_key=identity.key, p_hash=identity.body_hash, p_request_id=request_id, p_accepting=accepting)

    def reconcile_jobs(self, owner):
        rows = self.call('list_rfq_reconcile_requests', p_owner=str(owner), p_limit=50)
        try:
            if not isinstance(rows, list) or len(rows) > 50:
                raise ValueError('Invalid reconciliation requests')
            for row in rows:
                UUID(row['event_id'])
                UUID(row['draft_id'])
        except (TypeError, KeyError, ValueError, AttributeError):
            raise ServiceError('PROCESSING_UNAVAILABLE', '발송 대조 요청을 확인할 수 없습니다.', 503) from None
        return rows

    def finish_reconcile_job(self, owner, job, failed=False):
        if self.call('finish_rfq_reconcile_request', p_owner=str(owner), p_event=job['event_id'],
                     p_code='RECONCILE_UNAVAILABLE' if failed else None) is not True:
            raise ServiceError('PROCESSING_UNAVAILABLE', '발송 대조 완료를 기록할 수 없습니다.', 503)

    def recover_server(self, owner):
        result = self.call('recover_rfq_server', p_owner=str(owner))
        if (not isinstance(result, dict) or any(type(result.get(key)) is not int or result[key] < 0
                for key in ('interrupted_runs', 'uncertain_sends'))):
            raise ServiceError('PROCESSING_UNAVAILABLE', '중단 복구 상태를 확인할 수 없습니다.', 503)
        return result

    def require_http_schema(self):
        if self.call('rfq_http_health') != 'rfq-http-v1':
            raise ServiceError('PROCESSING_UNAVAILABLE', 'RFQ 승인·복구 DB 계약을 확인해야 합니다.', 503)

    def case(self, owner, case_id):
        try:
            rows = (self.client.table('work_cases').select(
                'id,mailbox_ref,input_revision,current_batch_id,current_rfq_round_id,status,processing_step,'
                'archived_at,decision,item_name,standardized_item_key,specification,quantity::text,unit,'
                'due_date,mandatory_conditions_jsonb,policy_jsonb')
                .eq('owner_user_id', str(owner)).eq('id', str(case_id)).limit(1).execute().data)
            if not isinstance(rows, list) or len(rows) > 1:
                raise ValueError('Invalid case response')
        except Exception:
            raise ServiceError('PROCESSING_UNAVAILABLE', 'RFQ 업무를 조회할 수 없습니다.', 503) from None
        if not rows:
            raise ServiceError('RESOURCE_NOT_FOUND', '업무를 찾을 수 없습니다.', 404)
        return rows[0]

    def step(self, owner, case, step, error=None):
        self.call('set_review_step', p_owner=str(owner), p_case=case['id'],
                  p_revision=case['input_revision'], p_batch=case['current_batch_id'], p_step=step, p_error=error)

    def candidate_run(self, owner, run_id):
        rows = self.data(self.client.table('agent_runs').select('id,work_case_id,input_revision,review_batch_id,status,agent_role,output_jsonb')
            .eq('owner_user_id', str(owner)).eq('id', str(UUID(str(run_id)))).limit(1))
        if len(rows) != 1:
            raise ServiceError('PROCESSING_UNAVAILABLE', '후보 선정 실행 근거를 확인할 수 없습니다.', 503)
        return rows[0]

    def save_draft(self, owner, case, target, recipient, subject, body, evidence, auto_eligible=False, settings_version=None, run_id=None, style=None):
        # Deterministic identity makes a lost INSERT acknowledgement recoverable.
        # DB draft_chain/rfq_draft gates lock the case/intent and enforce lineage.
        draft_id = str(uuid5(NAMESPACE_URL, 'gyeonjwo:rfq:' + target['intent_id']))
        row = dict(id=draft_id, owner_user_id=str(owner), work_case_id=case['id'],
            supplier_id=target['supplier_id'], intent_id=target['intent_id'], purpose='RFQ',
            recipient=recipient, subject=subject, body_text=body, input_revision=case['input_revision'],
            idempotency_key='rfq:' + target['intent_id'], evidence_refs_jsonb=evidence,
            style_snapshot_jsonb={'producer': 'rfq-v1', 'auto_policy_eligible': auto_eligible,
                                  'auto_settings_version': settings_version, 'producer_run_id': run_id,
                                  'mail_style': style or {}})
        try:
            self.client.table('mail_drafts').insert(row).execute()
        except Exception:
            # Do not retry INSERT blindly, replace a draft, or mint a new intent.
            existing = self.draft(owner, draft_id)
            for key in ('work_case_id', 'supplier_id', 'intent_id', 'purpose', 'recipient', 'input_revision', 'subject', 'body_text'):
                if existing.get(key) != row[key]:
                    raise ServiceError('REVISION_CONFLICT', '기존 RFQ 초안의 근거가 달라졌습니다.', 409) from None
        return draft_id

    def claim_send(self, owner, draft_id):
        result = self.call('claim_mail_send', p_owner=str(owner), p_draft=str(draft_id))
        if isinstance(result, dict) and result.get('authorized') is False and result.get('draft_id') == str(draft_id):
            return None
        try:
            claimed = AuthorizedMail.from_claim(draft_id, result)
            if not claimed.settings_version:
                raise ValueError('Missing authorized policy version')
            return claimed
        except (KeyError, ValueError, TypeError, AttributeError):
            # Claim may already have persisted SENDING. Never call Gmail or
            # release/reclaim it when the acknowledgement is malformed.
            raise ServiceError('PROCESSING_UNAVAILABLE', '발송 접수 결과를 확인할 수 없습니다.', 503) from None

    def finish_send(self, owner, draft_id, status, provider_id=None, code=None):
        if status not in {'SENT', 'FAILED', 'UNKNOWN'}:
            raise ValueError('Invalid send result')
        self.call('finish_mail_send', p_owner=str(owner), p_draft=str(draft_id),
                  p_result=status, p_provider_id=provider_id, p_error={'code': code} if code else None)

    def reconcile_sent(self, owner, draft_id, provider_id, evidence):
        self.call('reconcile_mail_send', p_owner=str(owner), p_draft=str(draft_id),
                  p_result='SENT', p_provider_id=provider_id, p_evidence=evidence)

    def pending(self, owner, limit=50, after=None):
        try:
            query = (self.client.table('mail_drafts').select('id,approved_by,approved_at,style_snapshot_jsonb')
                .eq('owner_user_id', str(owner)).eq('status', 'DRAFT')
                .or_('and(purpose.eq.RFQ,style_snapshot_jsonb->>producer.eq.rfq-v1),and(purpose.in.(CLARIFICATION,COUNTER_OFFER),style_snapshot_jsonb->>producer.eq.graph-review-v1)')
                .order('id').limit(limit))
            if after is not None:
                query = query.gt('id', str(UUID(str(after))))
            rows = query.execute().data
            if not isinstance(rows, list):
                raise ValueError('Invalid pending response')
            return rows
        except Exception:
            raise ServiceError('PROCESSING_UNAVAILABLE', '발송 대기 초안을 조회할 수 없습니다.', 503) from None
