from datetime import datetime, timezone
from app.api.responses import Envelope
from app.core.errors import ServiceError
from app.core.idempotency import request_identity
from app.core.request_context import current_request_id
from app.features.work_cases.application.cursor import utc_time, iso_z
from app.features.work_cases.domain.mail_drafts import DraftAccepted, snapshot_hash
from app.features.work_cases.domain.candidates import selected_for_dispatch, selection_staged

# 초안이 지금 어떤 발송 상태인지(send_state.code)와 화면 문구. 발송 워커(rfq_delivery.send)와 같은 기준으로 계산한다
SEND_STATE_MESSAGES = {
    'SENT': '보냈습니다.',
    'SENDING': '보내는 중입니다.',
    'AUTO_QUEUED': '자동 발송 대기 중입니다.',
    'APPROVED_QUEUED': '승인했습니다. 곧 보냅니다.',
    'APPROVAL_REQUIRED': '승인하면 보냅니다.',
    'CAUTION_APPROVAL_REQUIRED': '주의 표시가 있는 업체라 승인하면 보냅니다.',
    'SETTINGS_CHANGED': '초안을 만든 뒤 발송 설정이 바뀌었습니다. 승인하면 보냅니다.',
    'SEND_SETTINGS_REQUIRED': '발송 설정을 먼저 저장해 주세요.',
    'RECIPIENT_NOT_REGISTERED': '받는 주소가 거래처에 등록된 연락처가 아니라 보낼 수 없습니다.',
    'CANDIDATE_SELECTION_REQUIRED': '견적 요청 업체를 먼저 선택해 주세요.',
    'DRAFT_STALE': '업무 내용이 바뀌어 이 초안은 보내지 않습니다.',
    'SEND_RESULT_UNKNOWN': '발송 결과를 확인하지 못했습니다. 보낸편지함 대조가 필요합니다.',
    'SEND_SNAPSHOT_CHANGED': '보내기 직전에 초안 내용이 바뀌어 보내지 않았습니다.',
    'SEND_SETTINGS_CHANGED': '보내기 직전에 발송 설정이 바뀌어 보내지 않았습니다.',
    'SEND_FAILED': '보내지 못했습니다.',
}
# 승인·대조 요청 오류 코드별 문구
SUBMIT_ERROR_MESSAGES = {
    'RESOURCE_NOT_FOUND': '메일 초안을 찾을 수 없습니다.',
    'REVISION_CONFLICT': '업무 내용이 바뀌었습니다. 최신 초안을 확인해 주세요.',
    'DRAFT_CHANGED': '초안이 바뀌었습니다. 최신 초안을 확인해 주세요.',
    'SETTINGS_CHANGED': '발송 설정이 바뀌었습니다. 최신 설정을 확인한 뒤 다시 승인해 주세요.',
    'SEND_RESULT_UNKNOWN': '발송 결과를 확인하는 중인 초안입니다. 보낸편지함 대조를 먼저 해 주세요.',
    'ACTION_NOT_ALLOWED': '지금 업무 상태에서는 이 초안을 보낼 수 없습니다.',
    'REQUEST_IN_PROGRESS': '같은 요청을 처리하고 있습니다. 잠시 후 확인해 주세요.',
    'IDEMPOTENCY_KEY_REUSED': '같은 요청 키로 다른 내용을 보낼 수 없습니다.',
    'SEND_SCOPE_REQUIRED': '발송 설정이나 받는 주소를 확인해 주세요.',
    'PROCESSING_UNAVAILABLE': '발송 처리를 잠시 할 수 없습니다. 잠시 후 다시 시도해 주세요.',
}


def dispatch_state(draft, case, policy):
    """(auto_policy_eligible, approval_available) for API-16/17. Mirrors RfqDelivery's auto gate;
    the DB submit_rfq_draft_action still re-checks every condition on approval."""
    style = draft.get('style_snapshot_jsonb') or {}
    auto_eligible = (style.get('auto_policy_eligible') is True and not style.get('candidate_caution')
                     and policy.get('auto_eligible') is True)
    pending = (draft['status'] == 'DRAFT' and not draft.get('approved_by')
               and draft['input_revision'] == case['input_revision'])
    approval_available = pending and policy.get('configured') is True and (
        policy.get('approval_required') is True or not auto_eligible)
    return auto_eligible, approval_available


def send_state(owner, draft, case, policy, auto_eligible):
    """초안의 지금 발송 상태 {code, message}. 보내지 않은 초안은 발송 워커가 왜 보내지 않는지를,
    실패·불확실 초안은 저장된 실패 코드를 알려 준다."""
    status = draft['status']
    if status in {'SENT', 'SENDING'}:
        code = status
    elif status in {'FAILED', 'UNKNOWN'}:
        stored = (draft.get('error_jsonb') or {}).get('code')
        code = {'SETTINGS_CHANGED': 'SEND_SETTINGS_CHANGED', 'SEND_SNAPSHOT_CHANGED': 'SEND_SNAPSHOT_CHANGED',
                'SEND_RESULT_UNKNOWN': 'SEND_RESULT_UNKNOWN'}.get(stored, 'SEND_RESULT_UNKNOWN' if status == 'UNKNOWN'
                                                                   else 'SEND_FAILED')
    elif draft['input_revision'] != case['input_revision']:
        code = 'DRAFT_STALE'
    elif not selected_for_dispatch(draft, case):
        code = 'CANDIDATE_SELECTION_REQUIRED'
    elif draft.get('approved_by') == owner and draft.get('approved_at'):
        code = 'APPROVED_QUEUED'
    elif policy.get('configured') is not True:
        code = approval_block_reason(policy)
    elif auto_eligible:
        code = 'AUTO_QUEUED'
    else:
        style = draft.get('style_snapshot_jsonb') or {}
        if style.get('candidate_caution'):
            code = 'CAUTION_APPROVAL_REQUIRED'
        elif (style.get('auto_policy_eligible') is True and policy.get('auto_eligible') is True
              and policy.get('settings_version') != style.get('auto_settings_version')):
            code = 'SETTINGS_CHANGED'
        else:
            code = 'APPROVAL_REQUIRED'
    return {'code': code, 'message': SEND_STATE_MESSAGES[code]}


def approval_block_reason(policy):
    """Why sending is blocked by settings/recipient: FE opens mailbox settings only for the first."""
    if policy.get('configured') is True:
        return None
    if policy.get('settings_ready') is not True:
        return 'SEND_SETTINGS_REQUIRED'
    return 'RECIPIENT_NOT_REGISTERED'


class MailDraftService:
    def __init__(self, repo, codec):
        self.repo, self.codec = repo, codec

    def present(self, owner, draft, case):
        if selection_staged(draft):
            raise ServiceError('RESOURCE_NOT_FOUND', '메일 초안을 찾을 수 없습니다.', 404)
        policy = self.repo.send_policy(owner, case, draft['recipient'], draft['purpose'], draft['supplier_id'])
        auto_eligible, approval_available = dispatch_state(draft, case, policy)
        return {
            'draft_id': draft['id'], 'case_id': draft['work_case_id'], 'supplier_id': draft['supplier_id'],
            'purpose': draft['purpose'], 'recipient': draft['recipient'], 'subject': draft['subject'],
            'body_text': draft['body_text'], 'input_revision': draft['input_revision'],
            'evidence_refs': draft.get('evidence_refs_jsonb') or [],
            'snapshot_hash': snapshot_hash(draft), 'status': draft['status'],
            'replaces_id': draft.get('replaces_id'), 'is_stale': draft['input_revision'] != case['input_revision'],
            'effective_send_policy': {'configured': policy['configured'],
                'approval_required': policy.get('approval_required'), 'settings_version': policy.get('settings_version'),
                'source': policy.get('source'), 'source_settings_id': policy.get('source_settings_id'),
                'source_version': policy.get('source_version')},
            'provider_message_id': draft.get('sent_provider_message_id') if draft['status'] == 'SENT' else None,
            'sent_at': iso_z(utc_time(draft['sent_at'])) if draft.get('sent_at') else None,
            # KAN-167 S4-4: with approval OFF, drafts the worker will not auto-send
            # (selected before settings were saved, caution suppliers) still need a
            # manual approval path instead of a dead end.
            'auto_policy_eligible': auto_eligible,
            'approval_available': approval_available,
            'approval_block_reason': approval_block_reason(policy),
            'send_state': send_state(owner, draft, case, policy, auto_eligible),
        }

    def get(self, owner, draft_id):
        draft = self.repo.draft(owner, draft_id)
        case = self.repo.case(owner, draft['work_case_id'])
        return self.present(owner, draft, case)

    def list(self, owner, case_id, *, limit=25, cursor=None):
        case = self.repo.case(owner, case_id)
        scope = self.codec.scope(owner, 'MAIL_DRAFTS:' + str(case_id), False, '')
        snapshot, after = self.codec.decode(cursor, scope) if cursor else (iso_z(datetime.now(timezone.utc)), None)
        rows = self.repo.list_drafts(owner, case_id, snapshot, after, limit + 1)
        page = rows[:limit]
        next_cursor = self.codec.encode(scope, snapshot,
            (iso_z(utc_time(page[-1]['created_at'])), page[-1]['id'])) if len(rows) > limit else None
        return {'items': [self.present(owner, row, case) for row in page], 'next_cursor': next_cursor}

    def submit(self, owner, draft_id, action, payload, key, accepting):
        suffix = 'approve-and-send' if action == 'APPROVE_SEND' else 'reconcile'
        identity = request_identity(str(owner), 'POST', '/api/v1/mail-drafts/' + str(draft_id) + '/' + suffix,
                                    key, payload.model_dump(mode='json') if payload else {})
        if selection_staged(self.repo.draft(owner, draft_id)):
            raise ServiceError('RESOURCE_NOT_FOUND', '메일 초안을 찾을 수 없습니다.', 404)
        result = self.repo.submit_draft_action(owner, draft_id, action, payload, identity, current_request_id(), accepting)
        if isinstance(result, dict) and result.get('kind') == 'ERROR':
            code = result.get('code')
            statuses = {'RESOURCE_NOT_FOUND': 404, 'REVISION_CONFLICT': 409, 'DRAFT_CHANGED': 409,
                        'SETTINGS_CHANGED': 409, 'SEND_RESULT_UNKNOWN': 409, 'ACTION_NOT_ALLOWED': 409,
                        'REQUEST_IN_PROGRESS': 409, 'IDEMPOTENCY_KEY_REUSED': 409,
                        'SEND_SCOPE_REQUIRED': 422, 'PROCESSING_UNAVAILABLE': 503}
            if code in statuses:
                raise ServiceError(code, SUBMIT_ERROR_MESSAGES[code], statuses[code])
        try:
            if result['kind'] not in {'ACCEPTED', 'REPLAY'} or result['http_status'] != 202:
                raise ValueError('Invalid acceptance')
            envelope = Envelope[DraftAccepted].model_validate(result['response']).model_dump(mode='json')
            data = envelope['data']
            if (data['draft_id'] != str(draft_id) or data['accepted'] is not True
                    or data['draft_url'] != '/api/v1/mail-drafts/' + str(draft_id)
                    or not envelope['meta']['request_id']):
                raise ValueError('Invalid result scope')
            return envelope
        except (KeyError, ValueError, TypeError):
            raise ServiceError('PROCESSING_UNAVAILABLE', '메일 요청 접수 결과를 확인할 수 없습니다.', 503) from None
