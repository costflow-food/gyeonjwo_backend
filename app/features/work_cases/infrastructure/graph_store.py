"""Backend START stores for the team's unchanged graph/runner/tool protocols."""
from copy import deepcopy
from decimal import Decimal
from uuid import UUID
from app.core.errors import ServiceError
from app.features.work_cases.infrastructure.agent_store import AgentStore
from app.features.mail.application.attachments import fetch_gmail_attachment
from app.features.mail.domain.attachment_ids import part_reference
from app.features.mail_management.infrastructure.repository import MailManagementRepository
from app.features.work_cases.domain.rfq import mailbox_address

ITEM_COLUMNS = ('id,supplier_id,item_name,specification,unit,certifications_jsonb,origin,notes,last_purchase_id')


class GraphStore(AgentStore):
    def __init__(self, scope, job):
        super().__init__(scope, job)
        self.draft_ids, self.role_runs = set(), {}
        self.documents = {}
        self.ocr_pages = {}
        self.catalog_items, self.draft_records = {}, {}
        self.candidate_set_id = None
        self.extraction_rejections = {}
        self.active_extraction_run = None

    def current_input_revision(self, case_id):
        # Graph's changed() must be able to observe the NEW revision. Other
        # store methods still require the expected revision before reading data.
        self.scope.remaining_seconds()
        if str(case_id) != self.scope.case_id:
            raise ServiceError('ACTION_NOT_ALLOWED', '그래프 업무 범위를 벗어났습니다.', 409)
        return self.repo.revision(self.scope.owner, case_id)

    def rpc(self, name, **params):
        self.check(self.scope.case_id)
        return self.repo.call(name, p_job=self.job['job_id'], p_token=self.job['claim_token'],
                              p_revision=self.scope.revision, **params)

    def insert_quote_version(self, record, expected_input_revision):
        from app.agents.tools import QuoteVersionRecord
        self.check(record.work_case_id)
        if expected_input_revision != self.scope.revision or record.source_mail_id not in self.scope.mail_ids:
            raise ServiceError('REVISION_CONFLICT', '견적 입력 범위가 변경됐습니다.', 409)
        result = self.rpc('store_case_graph_quote', p_record=record.model_dump(mode='json'))
        if isinstance(result, dict) and result.get('kind') == 'STALE':
            return None
        if not isinstance(result, dict) or result.get('kind') != 'STORED':
            raise ServiceError('PROCESSING_UNAVAILABLE', '그래프 견적 저장을 확인할 수 없습니다.', 503)
        qid, saved = str(UUID(result['quote_id'])), QuoteVersionRecord.model_validate(result['record'])
        if (saved.work_case_id != record.work_case_id or saved.source_mail_id != record.source_mail_id
                or saved.supplier_id != record.supplier_id):
            raise ValueError('Invalid graph quote scope')
        self.records[qid] = saved
        self.saved_ids.add(qid)
        return qid, saved

    def extraction_issue_mail_ids(self):
        return self.scope.mail_ids

    def record_extraction_issue(self, case_id, agent_run_id, issue, supplier_name=None):
        self.check(case_id)
        run_id = self.active_extraction_run
        # The deployed tool passes mail IDs and a supplier name. PR #51 passes
        # the active run ID and a payload containing evidence references.
        if isinstance(agent_run_id, (list, tuple)):
            mail_ids = agent_run_id
        else:
            if run_id is None or str(agent_run_id) != run_id:
                raise ServiceError('ACTION_NOT_ALLOWED', '추출 거절 근거의 범위를 확인할 수 없습니다.', 409)
            if issue.get('code') == 'MISSING_FIELD' and issue.get('field') == 'supplier_name':
                # The existing DB contract acknowledges supplier matching
                # rejections only. The structured HOLD still carries MISSING_FIELD.
                return
            supplier_name = issue.get('supplier_name')
            evidence = issue.get('evidence')
            mail_ids = []
            for ref in evidence if isinstance(evidence, list) else []:
                evidence_id = ref.get('evidence_id') if isinstance(ref, dict) else None
                mail_id, _, location = evidence_id.partition(':') if isinstance(evidence_id, str) else ('', '', '')
                if not mail_id or not (location == 'body' or (location.startswith('part:') and location[5:])):
                    raise ServiceError('ACTION_NOT_ALLOWED', '추출 거절 근거의 범위를 확인할 수 없습니다.', 409)
                mail_ids.append(mail_id)
            mail_ids = list(dict.fromkeys(mail_ids))
        if (run_id is None or not mail_ids or not set(mail_ids) <= set(self.extraction_issue_mail_ids())
                or issue.get('field') != 'supplier_name'
                or issue.get('code') not in {'SUPPLIER_NOT_MATCHED', 'SUPPLIER_MISMATCH'}):
            raise ServiceError('ACTION_NOT_ALLOWED', '추출 거절 근거의 범위를 확인할 수 없습니다.', 409)
        for mail_id in mail_ids:
            try:
                result = self.rpc('record_case_graph_extraction_issue', p_run=run_id, p_issue={
                    'case_id': self.scope.case_id, 'mail_id': mail_id, 'batch_id': self.scope.batch_id,
                    'input_revision': self.scope.revision, 'code': issue['code'],
                    'field': issue['field'], 'supplier_name': supplier_name,
                })
            except ServiceError as error:
                if error.code == 'EXTRACTION_SCHEMA_REQUIRED':
                    # Optional evidence storage may deploy after the HOLD path.
                    return
                raise
            if result is not True:
                raise ServiceError('PROCESSING_UNAVAILABLE', '추출 거절 근거 저장을 확인할 수 없습니다.', 503)
        self.extraction_rejections.setdefault(run_id, set()).add(issue['code'])

    def start_agent_run(self, case_id, role, input_revision, input_snapshot, model_id, prompt_version):
        self.check(case_id)
        if input_revision != self.scope.revision:
            raise ServiceError('REVISION_CONFLICT', '역할 실행 중 업무 입력이 변경됐습니다.', 409)
        result = self.rpc('begin_case_graph_agent_run', p_role=str(role), p_input=input_snapshot,
                          p_model=model_id, p_prompt_version=prompt_version)
        run_id = str(UUID(result['agent_run_id']))
        if str(result.get('review_batch_id')) != self.scope.batch_id or run_id in self.role_runs:
            raise ValueError('Invalid graph role identity')
        self.role_runs[run_id] = str(role)
        if str(role) == 'INFO_EXTRACTOR':
            self.active_extraction_run = run_id
        return {'agent_run_id': run_id, 'review_batch_id': self.scope.batch_id}

    def finish_agent_run(self, agent_run_id, status, output, error):
        if str(agent_run_id) not in self.role_runs or status not in {'COMPLETED', 'FAILED'}:
            raise ServiceError('ACTION_NOT_ALLOWED', '그래프 역할 실행 범위를 확인할 수 없습니다.', 409)
        # Failed runner payloads contain partial_tool_calls with raw mail. Drop
        # them; only validated completed role output is private persisted data.
        safe_output = deepcopy(output) if status == 'COMPLETED' else None
        is_candidate = status == 'COMPLETED' and self.role_runs[str(agent_run_id)] == 'CANDIDATE'
        if is_candidate:
            snapshot = self.candidate_snapshot(str(agent_run_id), safe_output)
        if isinstance(safe_output, dict) and safe_output.get('next_action') == 'HOLD':
            from app.features.work_cases.domain.hold import from_agent_output
            safe_output['hold'] = from_agent_output(
                safe_output, rejection_codes=self.extraction_rejections.get(str(agent_run_id), ()),
            ).for_storage()
        if is_candidate:
            result = self.rpc('finish_case_graph_candidate_run', p_run=str(agent_run_id), p_output=safe_output,
                              p_candidate_set=snapshot.model_dump(mode='json'))
        else:
            result = self.rpc('finish_case_graph_agent_run', p_run=str(agent_run_id), p_status=status,
                              p_output=safe_output, p_error={'code': 'AGENT_FAILED'} if status == 'FAILED' else None)
        if result is not True:
            raise ServiceError('PROCESSING_UNAVAILABLE', '그래프 역할 결과 저장을 확인할 수 없습니다.', 503)
        if self.active_extraction_run == str(agent_run_id):
            self.active_extraction_run = None
            self.extraction_rejections.pop(str(agent_run_id), None)
        if is_candidate:
            self.candidate_set_id = str(agent_run_id)

    def candidate_snapshot(self, run_id, output):
        from app.agents.schemas import CandidateOut
        from app.features.work_cases.domain.candidates import CandidateSnapshot, CandidateItem, CandidateCheck
        parsed = CandidateOut.model_validate({key: value for key, value in output.items() if key in CandidateOut.model_fields})
        items = []
        for candidate in parsed.candidates:
            supplier = self.get_supplier(self.scope.case_id, candidate.supplier_id)
            if supplier is None:
                raise ValueError('Unowned candidate supplier')
            facts = []
            for ref in candidate.evidence_refs:
                if ref.startswith('supplier_items:'):
                    item_id = str(UUID(ref.split(':', 1)[1]))
                    row = self.catalog_items.get(item_id)
                    if row is None or row['supplier_id'] != candidate.supplier_id:
                        raise ValueError('Candidate evidence outside tool reads')
                    facts.append(row['item_name'] + (' (' + row['specification'] + ')' if row.get('specification') else ''))
            if not facts:
                raise ValueError('Missing handled-item evidence')
            try:
                contact = mailbox_address(supplier.get('contact_email'))
            except (ValueError, TypeError):
                contact = None
            score = Decimal(candidate.preference_score) if candidate.preference_score is not None else None
            checks = [CandidateCheck(label=check.condition, status={'PASS':'MET', 'FAIL':'UNMET', 'UNKNOWN':'UNKNOWN'}[str(check.state)],
                required=check.required, evidence_refs=check.evidence_refs) for check in candidate.checklist]
            items.append(CandidateItem(supplier_id=candidate.supplier_id, name=supplier['name'], contact_email=contact,
                handling_evidence='; '.join(dict.fromkeys(facts)), reason=candidate.reason, checks=checks,
                preference_score=int(score) if score is not None and score.is_finite() and score == score.to_integral_value() else None,
                preference_score_exact=candidate.preference_score, is_web_candidate=candidate.provisional,
                evidence_refs=candidate.evidence_refs, unknown_fields=[check.label for check in checks if check.status=='UNKNOWN'],
                caution=candidate.caution.model_dump() if candidate.caution else None))
        if str(parsed.next_action) == 'SEND_RFQ':
            ids = list(parsed.draft_ids)
            if not items or len(ids) != len(set(ids)) or any(value not in self.draft_records for value in ids):
                raise ValueError('Unconfirmed candidate proposal drafts')
            if {self.draft_records[value].supplier_id for value in ids} != {str(item.supplier_id) for item in items}:
                raise ValueError('Draft/candidate supplier mismatch')
        return CandidateSnapshot(case_id=self.scope.case_id, candidate_set_id=run_id, revision=self.scope.revision,
            review_batch_id=self.scope.batch_id, items=items, unknown_fields=parsed.unknown_fields,
            web_observations=parsed.web_observations)

    def get_candidate_conditions(self, case_id):
        return self.get_work_case(case_id)

    def incumbent_supplier_ids(self, case_id):
        case = self.get_work_case(case_id)
        # Reply suppliers must not become incumbents when REPLY is added later.
        rows = self.rows(self.repo.client.table('quote_versions').select('supplier_id')
            .eq('owner_user_id', self.scope.owner).eq('work_case_id', str(case_id))
            .eq('source_mail_id', case['initial_mail_id']).limit(1000))
        if len(rows) >= 1000:
            raise ValueError('Incumbent scan limit')
        return {row['supplier_id'] for row in rows}

    def search_supplier_items(self, case_id, keywords, limit):
        self.check(case_id)
        if not 1 <= limit <= 31 or not 1 <= len(keywords) <= 5:
            raise ValueError('Candidate search bounds')
        found = {}
        for word in keywords:
            if not isinstance(word, str) or not 1 <= len(word) <= 100:
                raise ValueError('Invalid candidate keyword')
            literal = word.replace('\\', '\\\\').replace('%', r'\%').replace('_', r'\_')
            rows = self.rows(self.repo.client.table('supplier_items').select(ITEM_COLUMNS)
                .eq('owner_user_id', self.scope.owner).ilike('item_name', '%' + literal + '%')
                .order('id').limit(limit))
            for row in rows:
                found[row['id']] = row
                self.catalog_items[row['id']] = deepcopy(row)
        return sorted(found.values(), key=lambda row: row['id'])[:limit]

    def get_supplier_items(self, case_id, ids):
        self.check(case_id)
        ids = list(dict.fromkeys(str(UUID(value)) for value in ids))
        if len(ids) > 155:
            raise ValueError('Candidate item bound')
        rows = self.rows(self.repo.client.table('supplier_items').select(ITEM_COLUMNS)
            .eq('owner_user_id', self.scope.owner).in_('id', ids)) if ids else []
        for row in rows:
            self.catalog_items[row['id']] = deepcopy(row)
        return rows

    def get_supplier(self, case_id, supplier_id):
        self.check(case_id)
        rows = self.rows(self.repo.client.table('suppliers').select('id,name,contact_email,evidence_jsonb,source_type')
            .eq('owner_user_id', self.scope.owner).eq('id', str(UUID(supplier_id)))
            .in_('source_type', ['REGISTERED', 'VERIFIED_WEB']).limit(1))
        return rows[0] if rows else None

    def list_purchase_history(self, case_id, supplier_item_ids):
        self.check(case_id)
        ids = [row['id'] for row in self.get_supplier_items(case_id, supplier_item_ids)]
        if not ids:
            return []
        # Only the latest real transaction per item is needed. Do not silently
        # truncate all history and mistake a partial scan for the latest record.
        result = []
        for item_id in ids:
            result.extend(self.rows(self.repo.client.table('purchase_history').select(
                'supplier_item_id,supplier_id,transaction_date,quantity::text,unit,unit_price::text,total_amount::text,currency')
                .eq('owner_user_id', self.scope.owner).eq('supplier_item_id', item_id)
                .order('transaction_date', desc=True).order('id', desc=True).limit(1)))
        return result

    def current_send_settings(self, case_id):
        case = self.get_work_case(case_id)
        base, override = MailManagementRepository(self.repo.client).settings(self.scope.owner, case['mailbox_ref'], case_id)
        return {**((base or {}).get('style_jsonb') or {}), **((override or {}).get('style_jsonb') or {})}

    def insert_mail_draft(self, record, expected_input_revision):
        from app.agents.tools import MailDraftRecord
        self.check(record.work_case_id)
        if expected_input_revision != self.scope.revision or record.purpose != 'RFQ' or record.replaces_id is not None:
            raise ServiceError('ACTION_NOT_ALLOWED', 'START 그래프의 RFQ 초안만 저장할 수 있습니다.', 409)
        supplier = self.get_supplier(record.work_case_id, record.supplier_id)
        if not supplier or mailbox_address(record.recipient) != mailbox_address(supplier['contact_email']):
            raise ServiceError('ACTION_NOT_ALLOWED', '초안의 등록 거래처 수신 주소를 확인할 수 없습니다.', 409)
        if '\r' in record.subject or '\n' in record.subject:
            raise ValueError('Invalid subject')
        # This is an unsendable private DRAFT proposal. Only API-12 may publish
        # selected suppliers into API-16/19 and the actual RFQ dispatch path.
        result = self.rpc('stage_case_graph_draft', p_record=record.model_dump(mode='json'))
        if isinstance(result, dict) and result.get('kind') == 'STALE':
            return None
        if not isinstance(result, dict) or result.get('kind') != 'STAGED':
            raise ValueError('Invalid graph draft result')
        draft_id = str(UUID(result['draft_id']))
        saved = MailDraftRecord.model_validate(result['record'])
        if saved.model_dump(mode='json') != record.model_dump(mode='json'):
            raise ValueError('Changed graph draft snapshot')
        self.draft_ids.add(draft_id)
        self.draft_records[draft_id] = saved
        return draft_id, saved

    def document_row(self, mail_id):
        if str(mail_id) not in self.scope.mail_ids:
            raise ServiceError('ACTION_NOT_ALLOWED', '그래프 원문 범위를 벗어났습니다.', 409)
        rows = self.rows(self.repo.client.table('mail_messages').select('id,attachments_jsonb,normalized_jsonb')
            .eq('owner_user_id', self.scope.owner).eq('work_case_id', self.scope.case_id).eq('id', str(mail_id)).limit(1))
        if not rows:
            raise ServiceError('RESOURCE_NOT_FOUND', '메일 원문을 찾을 수 없습니다.', 404)
        return rows[0]

    def list_unread_attachments(self, case_id, mail_ids):
        self.check(case_id)
        items = []
        for mail_id in dict.fromkeys(mail_ids):
            row = self.document_row(mail_id)
            for attachment in row.get('attachments_jsonb') or []:
                part = part_reference(attachment)
                if part is None:
                    continue
                existing = (row.get('normalized_jsonb') or {}).get('documents', {}).get(part)
                if isinstance(existing, dict):
                    self.cache_document(mail_id, part, existing)
                    continue
                items.append({'mail_id': str(mail_id), 'attachment_ref': part,
                    'filename': attachment.get('filename') or '', 'content_type': attachment.get('mime_type'),
                    'size_bytes': attachment.get('size_bytes')})
        return items

    def download_attachment(self, case_id, mail_id, attachment_ref):
        self.check(case_id)
        row = self.document_row(mail_id)
        if not any(part_reference(a) == attachment_ref for a in row.get('attachments_jsonb') or []):
            raise ServiceError('RESOURCE_NOT_FOUND', '메일 첨부를 찾을 수 없습니다.', 404)
        try:
            return fetch_gmail_attachment(self.scope.attachment_repo, self.scope.mailbox,
                self.scope.owner, mail_id, attachment_ref).content
        except ServiceError as error:
            if error.code in {'SOURCE_READ_FAILED', 'RESOURCE_NOT_FOUND'}:
                return None
            raise

    def cache_document(self, mail_id, part, document):
        if (document.get('status') not in {'READ', 'NO_TEXT', 'UNSUPPORTED', 'FAILED', 'TOO_LARGE'}
                or not isinstance(document.get('pages'), list) or any(not isinstance(p, str) for p in document['pages'])):
            raise ValueError('Invalid document result')
        self.documents[(str(mail_id), part)] = deepcopy(document)
        key = (str(mail_id), part)
        self.pages[key] = [self.ocr_pages.get((*key, page), {}).get('text', text)
                           for page, text in enumerate(document['pages'], 1)]
        self.check_document_budget()

    def save_document(self, case_id, mail_id, attachment_ref, document):
        self.check(case_id)
        self.document_row(mail_id)
        self.cache_document(mail_id, attachment_ref, document)
        result = self.rpc('save_case_graph_document', p_mail=str(mail_id), p_part=attachment_ref, p_document=document)
        if not isinstance(result, dict) or result.get('kind') != 'STORED':
            raise ValueError('Document persistence not confirmed')

    def list_case_mails(self, case_id):
        self.check(case_id)
        items = []
        for mail in self.mails(case_id):
            row = self.document_row(mail['id'])
            attachments = []
            for attachment in row.get('attachments_jsonb') or []:
                part = part_reference(attachment)
                if part is None:
                    continue
                doc = self.documents.get((mail['id'], part)) or (row.get('normalized_jsonb') or {}).get('documents', {}).get(part)
                if doc:
                    self.cache_document(mail['id'], part, doc)
                attachments.append({'attachment_ref': part, 'filename': attachment.get('filename'),
                    'pages': len(doc['pages']) if doc else 0, 'status': doc['status'] if doc else 'DOWNLOAD_FAILED'})
            items.append({**{key: mail[key] for key in ('subject', 'supplier_id', 'message_role', 'direction')},
                          'mail_id': mail['id'], 'received_at': mail['sent_or_received_at'], 'attachments': attachments})
        return items

    def read_mail_part(self, case_id, mail_id, part, page):
        self.check(case_id)
        if part == 'body':
            return super().read_mail_part(case_id, mail_id, part, page)
        row = self.document_row(mail_id)
        doc = self.documents.get((str(mail_id), part)) or (row.get('normalized_jsonb') or {}).get('documents', {}).get(part)
        if not doc:
            return None
        self.cache_document(mail_id, part, doc)
        if doc['status'] not in {'READ', 'NO_TEXT'} or type(page) is not int or not 1 <= page <= len(doc['pages']):
            return None
        location = f'{part}:page:{page}'
        text = doc['pages'][page - 1]
        if text.strip():
            return {'text': text, 'location': location}
        key = (str(mail_id), part, page)
        if key not in self.ocr_pages:
            from app.features.review.infrastructure.documents import ocr_pdf_page
            content = self.download_attachment(case_id, mail_id, part)
            result = (ocr_pdf_page(content, page, remaining_seconds=self.scope.remaining_seconds) if content else
                      {'status': 'FAILED', 'text': '', 'attempts': 0, 'retry_exhausted': False,
                       'error_kind': 'SOURCE_READ_FAILED'})
            self.ocr_pages[key] = {'text': result['text'] if result['status'] == 'READ' else '',
                                   'location': location, 'text_source': 'OCR',
                                   'unreadable': result['status'] != 'READ', 'attempts': result['attempts'],
                                   'retry_exhausted': result['retry_exhausted'], 'error_kind': result['error_kind']}
            self.cache_document(mail_id, part, doc)
        return dict(self.ocr_pages[key])
