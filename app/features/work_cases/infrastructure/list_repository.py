"""Read-only MVP adapter, without new SQL/RPC installation.

Read owner-filtered keyset batches, then join only owner-filtered initial-mail
metadata. Searching/sorting derived mail fields is done in the service. This costs
O(owner's cases) per page for exact cross-table search/counts; fail explicitly above
10,000 cases rather than return incomplete counts. A future single SQL read RPC can
replace this adapter after review with the DB owner. Never select body_text/tokens.
"""
from app.core.errors import ServiceError
from uuid import UUID
from app.features.work_cases.domain.replies import reply_summary

CASE_COLUMNS = 'id,initial_mail_id,created_at,status,processing_step,input_revision,archived_at,item_name,decision'
MAIL_COLUMNS = 'id,subject,sender,sent_or_received_at,attachments_jsonb,supplier_id'
BATCH_SIZE = 200
MAX_CASES = 10000


class WorkCaseListRepository:
    def __init__(self, client, *, extraction_fields=True):
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
                try:
                    batch = query.execute().data
                except Exception as error:
                    if (self.extraction_fields and getattr(error, 'code', None) == '42703'
                            and 'requester_name' in str(getattr(error, 'message', ''))):
                        # Read stored names independently of whether new extraction
                        # is enabled. Older schemas remain readable without migration.
                        self.extraction_fields = False
                        return self.rows(owner, snapshot, archived)
                    raise
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

    def reply_summaries(self, owner, case_ids):
        """One owner-scoped read RPC for the page; no per-row HTTP or mail reads.

        The DB owner must supply the current RFQ round and verified reply links.
        Never infer round membership from receipt time or a matching supplier.
        """
        if not isinstance(case_ids, (list, tuple)) or len(case_ids) > 100:
            raise ServiceError('PROCESSING_UNAVAILABLE', '회신 집계 조회 범위를 확인할 수 없습니다.', 503)
        if not case_ids:
            return {}
        try:
            owner_id = str(UUID(str(owner)))
            ordered = list(dict.fromkeys(str(UUID(str(value))) for value in case_ids))
            expected = set(ordered)
            try:
                result = self.client.rpc('get_work_case_reply_facts', {
                    'p_owner': owner_id, 'p_case_ids': ordered,
                }).execute().data
            except Exception as error:
                # SQL v3.6 uses P0002 for both absent and foreign cases.
                # Never inspect or expose database exception messages.
                if getattr(error, 'code', None) in {'P0002', 'PT404'}:
                    raise ServiceError('RESOURCE_NOT_FOUND', '업무를 찾을 수 없습니다.', 404) from None
                if getattr(error, 'code', None) == 'PGRST202':
                    raise ServiceError('PROCESSING_UNAVAILABLE', '회신 집계 DB 함수를 먼저 적용해야 합니다.', 503,
                                       retryable=False, next_action='CONTACT_SERVER_ADMIN') from None
                raise
            if (not isinstance(result, dict) or type(result.get('contract_version')) is not int
                    or result['contract_version'] != 1
                    or str(result.get('owner_user_id')) != owner_id
                    or not isinstance(result.get('cases'), list)):
                raise ValueError('Invalid reply facts response')
            summaries = {}
            for row in result['cases']:
                case_id = str(UUID(row['case_id']))
                if case_id not in expected or case_id in summaries:
                    raise ValueError('Invalid reply case scope')
                sent, replied = row['sent_supplier_ids'], row['valid_reply_supplier_ids']
                round_id = row['request_round_id']
                if round_id is None:
                    if sent != [] or replied != []:
                        raise ValueError('Reply facts without a round')
                else:
                    UUID(round_id)
                # v3.6 guarantees this relationship. Do not hide corrupt facts
                # by silently counting only their intersection.
                if not isinstance(sent, list) or not isinstance(replied, list):
                    raise ValueError('Invalid reply facts')
                if not {str(UUID(value)) for value in replied} <= {str(UUID(value)) for value in sent}:
                    raise ValueError('Reply supplier without successful RFQ')
                summaries[case_id] = reply_summary(sent, replied)
            if summaries.keys() != expected:
                raise ValueError('Incomplete reply facts')
            return summaries
        except ServiceError:
            raise
        except Exception:
            # Missing infrastructure is not a proven 0/0. No provider details or
            # contact information may escape through errors or logs.
            raise ServiceError('PROCESSING_UNAVAILABLE', '견적 회신 집계를 조회할 수 없습니다.', 503) from None
