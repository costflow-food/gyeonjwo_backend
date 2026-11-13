"""Owner-scoped reads and single-transaction mutation adapters."""
import re
from datetime import datetime, timezone
from uuid import UUID
from app.core.errors import ServiceError
from app.features.work_cases.infrastructure.list_repository import WorkCaseListRepository

MAIL_COLUMNS = ('id,owner_user_id,mailbox_ref,work_case_id,subject,sender,body_text,sent_or_received_at,'
                'attachments_jsonb,mail_classification,classification_reason,processing_status,link_status,'
                'normalized_jsonb,rfc_in_reply_to,rfc_references')
SETTINGS_COLUMNS = ('id,version_no,configured,approval_required_by_purpose,allowed_recipients_jsonb,'
                    'allowed_scope_jsonb,style_jsonb,effective_at')


class MailManagementRepository:
    def __init__(self, client):
        self.client = client

    def rows(self, query):
        try:
            rows = query.execute().data
            if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
                raise ValueError('Invalid rows')
            return rows
        except Exception:
            raise ServiceError('PROCESSING_UNAVAILABLE', '메일 관리 정보를 조회할 수 없습니다.', 503) from None

    def pending_query(self, owner):
        # Exactly the same population as API-01 intake_pending.
        return (self.client.table('mail_messages').select(MAIL_COLUMNS)
                .eq('owner_user_id', str(owner)).is_('work_case_id', 'null')
                .eq('processing_status', 'NEEDS_REVIEW'))

    def pending(self, owner, snapshot, after, limit):
        query = (self.pending_query(owner).lte('created_at', snapshot)
                 .order('sent_or_received_at', desc=True).order('id', desc=True).limit(limit))
        reason_filter = 'mail_classification.eq.UNCERTAIN,link_status.eq.AMBIGUOUS'
        if after:
            boundary = f'sent_or_received_at.lt.{after[0]},and(sent_or_received_at.eq.{after[0]},id.lt.{after[1]})'
            query = query.or_(f'and(or({reason_filter}),or({boundary}))')
        else:
            query = query.or_(reason_filter)
        return self.rows(query)

    def total_pending(self, owner):
        return WorkCaseListRepository(self.client).intake_pending(owner)

    def mail(self, owner, mail_id):
        rows = self.rows(self.client.table('mail_messages').select(MAIL_COLUMNS)
                         .eq('owner_user_id', str(owner)).eq('id', str(mail_id)).limit(1))
        if not rows:
            raise ServiceError('RESOURCE_NOT_FOUND', '메일을 찾을 수 없습니다.', 404)
        return rows[0]

    def candidates(self, owner, mail):
        """Find manual candidates from outbound or supplier initial RFC evidence."""
        references = sorted(set(re.findall(r'<[^<>\s]{1,998}>',
                                (mail.get('rfc_in_reply_to') or '') + ' ' + (mail.get('rfc_references') or ''))))
        if not references:
            return []
        if len(references) > 100:
            raise ServiceError('PROCESSING_UNAVAILABLE', '회신 연결 근거 범위를 확인해야 합니다.', 503)
        messages = self.rows(self.client.table('mail_messages').select('id,work_case_id,direction,sender')
                             .eq('owner_user_id', str(owner)).eq('mailbox_ref', mail['mailbox_ref'])
                             .or_('direction.eq.OUT,and(direction.eq.IN,message_role.eq.INITIAL)')
                             .in_('rfc_message_id', references).limit(101))
        if len(messages) > 100:
            raise ServiceError('PROCESSING_UNAVAILABLE', '회신 연결 후보 범위를 확인해야 합니다.', 503)
        outbound_cases = sorted({str(UUID(row['work_case_id'])) for row in messages
                                 if row['direction'] == 'OUT' and row.get('work_case_id')})
        sender = (mail.get('sender') or '').strip().lower()
        initial_mails = sorted({str(UUID(row['id'])) for row in messages
                                if row['direction'] == 'IN' and sender
                                and (row.get('sender') or '').strip().lower() == sender})
        membership = []
        if outbound_cases:
            membership.append(f"id.in.({','.join(outbound_cases)})")
        if initial_mails:
            membership.append(f"initial_mail_id.in.({','.join(initial_mails)})")
        if not membership:
            return []
        cases = self.rows(self.client.table('work_cases').select('id,status,item_name,requester_name,initial_mail_id')
                          .eq('owner_user_id', str(owner)).eq('mailbox_ref', mail['mailbox_ref'])
                          .or_(','.join(membership)).is_('archived_at', 'null').limit(101))
        if len(cases) > 100:
            raise ServiceError('PROCESSING_UNAVAILABLE', '회신 연결 후보 범위를 확인해야 합니다.', 503)
        return cases

    def scope(self, owner, mailbox_ref=None, case_id=None):
        case = None
        if case_id:
            rows = self.rows(self.client.table('work_cases').select('id,mailbox_ref,input_revision,archived_at')
                             .eq('owner_user_id', str(owner)).eq('id', str(case_id)).limit(1))
            if not rows or (mailbox_ref is not None and rows[0]['mailbox_ref'] != mailbox_ref):
                raise ServiceError('RESOURCE_NOT_FOUND', '업무 또는 메일함을 찾을 수 없습니다.', 404)
            case = rows[0]
            mailbox_ref = case['mailbox_ref']
        query = self.client.table('mail_sync_states').select('mailbox_ref').eq('owner_user_id', str(owner))
        if mailbox_ref is not None:
            query = query.eq('mailbox_ref', mailbox_ref)
        rows = self.rows(query.order('mailbox_ref').limit(2))
        if not rows:
            raise ServiceError('RESOURCE_NOT_FOUND', '연결된 메일함을 찾을 수 없습니다.', 404)
        if len(rows) != 1:
            raise ServiceError('INVALID_REQUEST', '설정할 메일함을 선택해 주세요.', 422, fields=['query.mailbox_ref'])
        return rows[0]['mailbox_ref'], case

    def settings(self, owner, mailbox_ref, case_id):
        def query():
            return (self.client.table('mail_send_settings').select(SETTINGS_COLUMNS)
                    .eq('owner_user_id', str(owner)).eq('mailbox_ref', mailbox_ref)
                    .lte('effective_at', datetime.now(timezone.utc).isoformat())
                    .order('effective_at', desc=True).order('version_no', desc=True).order('id', desc=True).limit(1))
        base = self.rows(query().is_('work_case_id', 'null'))
        override = self.rows(query().eq('work_case_id', str(case_id))) if case_id else []
        return (base[0] if base else None, override[0] if override else None)

    def suppliers(self, owner):
        rows = self.rows(self.client.table('suppliers').select('id,name,contact_email')
                         .eq('owner_user_id', str(owner)).in_('source_type', ['REGISTERED', 'VERIFIED_WEB'])
                         .order('name').order('id').limit(1000))
        if len(rows) >= 1000:
            raise ServiceError('PROCESSING_UNAVAILABLE', '거래처 설정 목록의 조회 범위를 확인해야 합니다.', 503)
        return rows

    def drafts(self, owner, case_id):
        if case_id is None:
            return []
        rows = self.rows(self.client.table('mail_drafts').select('id,replaces_id,supplier_id,purpose,recipient,status,input_revision,style_snapshot_jsonb')
                         .eq('owner_user_id', str(owner)).eq('work_case_id', str(case_id))
                         .order('created_at', desc=True).order('id', desc=True).limit(1000))
        if len(rows) >= 1000:
            raise ServiceError('PROCESSING_UNAVAILABLE', '초안 설정 목록의 조회 범위를 확인해야 합니다.', 503)
        replaced = {row['replaces_id'] for row in rows if row.get('replaces_id')}
        from app.features.work_cases.domain.candidates import selection_staged
        return [row for row in rows if row['id'] not in replaced and not selection_staged(row)]

    def call(self, name, params, integration_code):
        try:
            result = self.client.rpc(name, params).execute().data
        except Exception as error:
            if getattr(error, 'code', None) == 'PGRST202':
                raise ServiceError(integration_code, '이 작업의 저장 함수가 아직 연결되지 않았습니다.', 503,
                                   retryable=False, next_action='CONTACT_SERVER_ADMIN') from None
            raise ServiceError('PROCESSING_UNAVAILABLE', '메일 관리 요청을 저장할 수 없습니다.', 503) from None
        if isinstance(result, dict) and result.get('kind') == 'ERROR':
            statuses = {'RESOURCE_NOT_FOUND': 404, 'REVISION_CONFLICT': 409, 'SETTINGS_CHANGED': 409,
                        'DRAFT_CHANGED': 409, 'SEND_RESULT_UNKNOWN': 409, 'ACTION_NOT_ALLOWED': 409,
                        'REQUEST_IN_PROGRESS': 409, 'IDEMPOTENCY_KEY_REUSED': 409,
                        'INVALID_LINK_TARGET': 422, 'SEND_SCOPE_REQUIRED': 422}
            code = result.get('code')
            if code in statuses:
                raise ServiceError(code, '최신 메일·업무·설정을 확인해 주세요.', statuses[code], retryable=False)
            raise ServiceError('PROCESSING_UNAVAILABLE', '요청 처리 결과를 확인할 수 없습니다.', 503)
        return result

    def receipt(self, identity, integration_code):
        result = self.call('lookup_mail_management_request', {
            'p_owner': identity.user_id, 'p_method': identity.method, 'p_path': identity.path,
            'p_key': identity.key, 'p_hash': identity.body_hash}, integration_code)
        if isinstance(result, dict) and result.get('kind') in {'NONE', 'REPLAY'}:
            return result if result['kind'] == 'REPLAY' else None
        raise ServiceError('PROCESSING_UNAVAILABLE', '저장된 요청 이력을 확인할 수 없습니다.', 503)

    def resolve(self, owner, mail_id, body, snapshot, identity, request_id):
        return self.call('submit_mail_intake_resolution', {
            'p_owner': str(owner), 'p_mail': str(mail_id), 'p_body': body, 'p_snapshot': snapshot,
            'p_key': identity.key, 'p_hash': identity.body_hash, 'p_request_id': request_id}, 'INTAKE_INTEGRATION_REQUIRED')

    def save_settings(self, owner, body, snapshot, identity, request_id):
        return self.call('submit_mail_send_settings', {
            'p_owner': str(owner), 'p_body': body, 'p_snapshot': snapshot,
            'p_key': identity.key, 'p_hash': identity.body_hash, 'p_request_id': request_id}, 'SETTINGS_INTEGRATION_REQUIRED')
