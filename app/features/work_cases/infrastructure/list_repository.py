"""Read-only MVP adapter, without new SQL/RPC installation.

Read owner-filtered keyset batches, then join only owner-filtered initial-mail
metadata. Searching/sorting derived mail fields is done in the service. This costs
O(owner's cases) per page for exact cross-table search/counts; fail explicitly above
10,000 cases rather than return incomplete counts. A future single SQL read RPC can
replace this adapter after review with the DB owner. Never select body_text/tokens.
"""
from app.core.errors import ServiceError

CASE_COLUMNS = 'id,initial_mail_id,created_at,status,processing_step,input_revision,archived_at,item_name,decision'
MAIL_COLUMNS = 'id,subject,sender,sent_or_received_at,attachments_jsonb,supplier_id'
BATCH_SIZE = 200
MAX_CASES = 10000


class WorkCaseListRepository:
    def __init__(self, client, *, extraction_fields=False):
        self.client, self.extraction_fields = client, extraction_fields

    def rows(self, owner, snapshot, archived):
        cases, after = [], None
        try:
            while True:
                columns = CASE_COLUMNS + (',requester_name' if self.extraction_fields else '')
                query = self.client.table('work_cases').select(columns).eq('owner_user_id', str(owner))
                query = query.lte('created_at', snapshot).order('id').limit(BATCH_SIZE)
                query = query.not_.is_('archived_at', 'null') if archived else query.is_('archived_at', 'null')
                if after:
                    query = query.gt('id', after)
                batch = query.execute().data
                if not isinstance(batch, list):
                    raise ValueError('Invalid work-case response')
                cases.extend(batch)
                if len(cases) > MAX_CASES:
                    raise ServiceError('PROCESSING_UNAVAILABLE', '목록 조회 범위 최적화가 필요합니다.', 503)
                if len(batch) < BATCH_SIZE:
                    break
                next_id = batch[-1]['id']
                if after is not None and next_id <= after:
                    raise ValueError('Unstable repository pagination')
                after = next_id
            mails = {}
            ids = sorted({row['initial_mail_id'] for row in cases})
            for index in range(0, len(ids), BATCH_SIZE):
                rows = (self.client.table('mail_messages').select(MAIL_COLUMNS)
                        .eq('owner_user_id', str(owner)).in_('id', ids[index:index + BATCH_SIZE]).execute().data)
                for row in rows:
                    mails[row['id']] = row
            supplier_ids = sorted({m['supplier_id'] for m in mails.values() if m.get('supplier_id')})
            names = {}
            for index in range(0, len(supplier_ids), BATCH_SIZE):
                rows = (self.client.table('suppliers').select('id,name').eq('owner_user_id', str(owner))
                        .in_('id', supplier_ids[index:index + BATCH_SIZE])
                        .in_('source_type', ['REGISTERED', 'VERIFIED_WEB']).execute().data)
                names.update({row['id']: row['name'] for row in rows})
            for case in cases:
                mail = mails.get(case['initial_mail_id'], {})
                if case['processing_step'] != 'PENDING_START':
                    case['registered_requester_name'] = names.get(mail.get('supplier_id'))
            # Hide cross-owner or missing initial messages rather than expose a case's
            # data without proving ownership of BOTH sides of the relationship.
            return [(case, mails[case['initial_mail_id']]) for case in cases if case['initial_mail_id'] in mails]
        except ServiceError:
            raise
        except Exception:
            raise ServiceError('PROCESSING_UNAVAILABLE', '업무 목록을 조회할 수 없습니다.', 503) from None

    def intake_pending(self, owner):
        try:
            response = (self.client.table('mail_messages').select('id', count='exact', head=True)
                        .eq('owner_user_id', str(owner)).is_('work_case_id', 'null')
                        .eq('processing_status', 'NEEDS_REVIEW')
                        .or_('mail_classification.eq.UNCERTAIN,link_status.eq.AMBIGUOUS').execute())
            if type(response.count) is not int or response.count < 0:
                raise ValueError('Invalid pending count')
            return response.count
        except Exception:
            raise ServiceError('PROCESSING_UNAVAILABLE', '접수 확인 건수를 조회할 수 없습니다.', 503) from None
