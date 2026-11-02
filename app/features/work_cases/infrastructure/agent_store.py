"""Backend implementations of the agent team's CaseStore/QuoteStore/AgentRunStore.

Tool calls persist full immutable quotes via one service-only RPC. Agent callbacks
are scoped through a ContextVar proxy; no per-user global store replacement.
"""
from copy import deepcopy
from uuid import UUID
from app.core.errors import ServiceError
from app.features.mail.application.attachments import fetch_gmail_attachment
from app.features.mail.domain.attachment_ids import part_reference
from app.features.review.infrastructure.documents import read_pdf_pages


class AgentStore:
    def __init__(self, scope, job):
        self.scope, self.repo, self.job = scope, scope.repo, job
        self.saved_ids, self.records, self.pages = set(), {}, {}
        self.body_lengths = {}
        self.final_output = None
        self.model_id, self.prompt_version = '', ''

    def check(self, case_id):
        self.scope.check(str(case_id), self.scope.revision)

    def rows(self, query):
        self.scope.remaining_seconds()
        try:
            rows = query.execute().data
            if not isinstance(rows, list):
                raise ValueError('Invalid rows')
            return rows
        except ServiceError:
            raise
        except Exception:
            raise ServiceError('PROCESSING_UNAVAILABLE', '에이전트 저장소 조회에 실패했습니다.', 503) from None

    def current_input_revision(self, case_id):
        self.check(case_id)
        return self.repo.revision(self.scope.owner, case_id)

    def get_work_case(self, case_id):
        self.check(case_id)
        row = self.repo.context(self.scope.owner, case_id, self.scope.revision, self.scope.batch_id)
        return {**row, 'mandatory_conditions': row.get('mandatory_conditions_jsonb') or {},
                'policy': row.get('policy_jsonb') or {}, 'latest_quote_version_ids': self.latest_ids(case_id)}

    def mails(self, case_id):
        self.check(case_id)
        return self.rows(self.repo.client.table('mail_messages').select(
            'id,subject,body_text,sender,attachments_jsonb,supplier_id,message_role,direction,sent_or_received_at')
            .eq('owner_user_id', self.scope.owner).eq('work_case_id', str(case_id))
            .in_('id', list(self.scope.mail_ids)))

    def list_case_mails(self, case_id):
        return [{'mail_id':m['id'], 'subject':m['subject'], 'supplier_id':m.get('supplier_id'),
                 'message_role':m['message_role'], 'direction':m['direction'], 'received_at':m['sent_or_received_at'],
                 'attachments':[{'attachment_ref':part_reference(a), 'filename':a.get('filename'),
                    'pages':len(self.document(m['id'], a)) if a.get('mime_type')=='application/pdf' else 0,
                    'status':'READ' if a.get('mime_type')=='application/pdf' and self.document(m['id'],a) else 'UNSUPPORTED'}
                    for a in m.get('attachments_jsonb') or [] if part_reference(a) is not None]}
                for m in self.mails(case_id)]

    def document(self, mail_id, attachment):
        key=(mail_id, part_reference(attachment))
        if key not in self.pages:
            try:
                content=fetch_gmail_attachment(self.scope.attachment_repo,self.scope.mailbox,
                    self.scope.owner,mail_id,key[1])
                self.pages[key]=read_pdf_pages(content.content)
            except ValueError:
                self.pages[key]=[]
            except ServiceError as error:
                if error.code in {'MAILBOX_RECONNECT_REQUIRED','EXTRACTION_TIME_LIMIT'}:
                    raise
                self.pages[key]=[]
            self.scope.remaining_seconds()
            if sum(self.body_lengths.values())+sum(len(t) for pages in self.pages.values() for t in pages)>self.scope.max_characters:
                raise ServiceError('EXTRACTION_DOCUMENT_LIMIT','문서 처리 한도를 초과했습니다.',503)
        return self.pages[key]

    def read_mail_part(self, case_id, mail_id, part, page):
        self.check(case_id)
        mail=next((m for m in self.mails(case_id) if m['id']==str(mail_id)),None)
        if mail is None:
            return None
        if part=='body':
            if page is not None:
                return None
            text=mail.get('body_text') or ''
            self.body_lengths[mail_id]=len(text)
            if sum(self.body_lengths.values())+sum(len(t) for pages in self.pages.values() for t in pages)>self.scope.max_characters:
                raise ServiceError('EXTRACTION_DOCUMENT_LIMIT','문서 처리 한도를 초과했습니다.',503)
            return {'text':text,'location':'body'}
        for a in mail.get('attachments_jsonb') or []:
            if part_reference(a)==part and a.get('mime_type')=='application/pdf':
                pages=self.document(mail_id,a)
                if type(page) is int and 1<=page<=len(pages):
                    return {'text':pages[page-1],'location':f'{part}:page:{page}'}
        return None

    def resolve_supplier(self, case_id, supplier_name):
        self.check(case_id)
        rows=self.rows(self.repo.client.table('suppliers').select('id')
            .eq('owner_user_id',self.scope.owner).eq('name',supplier_name)
            .in_('source_type',['REGISTERED','VERIFIED_WEB']).limit(2))
        return rows[0]['id'] if len(rows)==1 else None

    def latest_ids(self, case_id):
        self.check(case_id)
        rows=self.rows(self.repo.client.table('quote_versions').select('id,supplier_id,version_no')
            .eq('owner_user_id',self.scope.owner).eq('work_case_id',str(case_id)).order('version_no',desc=True).limit(1000))
        if len(rows)>=1000:
            raise ServiceError('PROCESSING_UNAVAILABLE','견적 조회 범위 최적화가 필요합니다.',503)
        latest={}
        for row in rows:
            latest.setdefault(row['supplier_id'],row['id'])
        return list(latest.values())

    def latest_quote_version(self, case_id, supplier_id):
        from app.agents.tools import QuoteVersionRecord
        self.check(case_id)
        columns=('id,work_case_id,supplier_id,source_mail_id,version_no,supersedes_id,unit_price::text,unit,'
                 'shipping_cost::text,available_quantity::text,effective_from,valid_until,checks_jsonb,'
                 'pricing_terms_jsonb,cost_terms_jsonb,item_notes_jsonb,evidence_jsonb,extraction_status,currency')
        rows=self.rows(self.repo.client.table('quote_versions').select(columns)
            .eq('owner_user_id',self.scope.owner).eq('work_case_id',str(case_id))
            .eq('supplier_id',str(supplier_id)).order('version_no',desc=True).limit(1))
        if not rows:
            return None
        row=dict(rows[0]); qid=row.pop('id')
        saved=row['pricing_terms_jsonb'].get('agent_record')
        if saved is not None:
            row=deepcopy(saved)
        else:
            # Legacy JSON objects retain all content in metadata; only known
            # collection wrappers are interpreted as agent lists.
            for key in ('cost_terms_jsonb','item_notes_jsonb'):
                value=row[key]
                if isinstance(value,dict):
                    if value and 'items' not in value:
                        raise ServiceError('QUOTE_CONTRACT_REQUIRED','기존 견적 저장 형식을 확인해야 합니다.',503)
                    row[key]=value.get('items',[])
            row.update(mail_normalized={},case_conditions=None)
        record=QuoteVersionRecord.model_validate(row)
        self.records[qid]=record
        return qid,record

    def insert_quote_version(self, record, expected_input_revision):
        self.check(record.work_case_id)
        if expected_input_revision!=self.scope.revision or record.source_mail_id not in self.scope.mail_ids:
            raise ServiceError('REVISION_CONFLICT','추출 입력 범위가 변경됐습니다.',409)
        result=self.repo.call('store_case_agent_quote',p_job=self.job['job_id'],p_token=self.job['claim_token'],
            p_revision=self.scope.revision,p_record=record.model_dump(mode='json'))
        if not isinstance(result,dict) or result.get('kind') not in {'STORED','STALE'}:
            raise ServiceError('PROCESSING_UNAVAILABLE','견적 저장 응답을 확인할 수 없습니다.',503)
        if result['kind']=='STALE':
            return None
        qid=str(UUID(result['quote_id']))
        from app.agents.tools import QuoteVersionRecord
        saved=QuoteVersionRecord.model_validate(result['record'])
        if (saved.work_case_id!=record.work_case_id or saved.supplier_id!=record.supplier_id
                or saved.source_mail_id!=record.source_mail_id):
            raise ServiceError('PROCESSING_UNAVAILABLE','견적 저장 범위를 확인할 수 없습니다.',503)
        self.saved_ids.add(qid); self.records[qid]=saved
        return qid,saved

    def start_agent_run(self, case_id, role, input_revision, input_snapshot, model_id, prompt_version):
        self.check(case_id)
        if role!='INFO_EXTRACTOR' or input_revision!=self.scope.revision:
            raise ServiceError('ACTION_NOT_ALLOWED','① 정보 추출만 실행할 수 있습니다.',409)
        started=self.repo.call('begin_case_agent_run',p_job=self.job['job_id'],p_token=self.job['claim_token'],
            p_revision=self.scope.revision,p_model=model_id,p_prompt_version=prompt_version,p_input=input_snapshot)
        if started is not True:
            raise ServiceError('PROCESSING_UNAVAILABLE','실행 기록을 확인할 수 없습니다.',503)
        self.model_id,self.prompt_version=model_id,prompt_version
        return {'agent_run_id':self.job['job_id'],'review_batch_id':self.scope.batch_id}

    def finish_agent_run(self, agent_run_id, status, output, error):
        self.check(self.scope.case_id)
        if str(agent_run_id)!=str(self.job['job_id']):
            raise ServiceError('ACTION_NOT_ALLOWED','실행 범위를 벗어났습니다.',409)
        # Never persist AgentRunner.partial_tool_calls: they contain raw mail.
        self.final_output=deepcopy(output) if status=='COMPLETED' else None


class ScopedStore:
    def __getattr__(self, name):
        from app.features.work_cases.application.extraction_scope import CURRENT_SCOPE
        scope=CURRENT_SCOPE.get(None)
        if scope is None or getattr(scope,'agent_store',None) is None:
            raise ServiceError('ACTION_NOT_ALLOWED','활성 추출 실행이 없습니다.',409)
        return getattr(scope.agent_store,name)
