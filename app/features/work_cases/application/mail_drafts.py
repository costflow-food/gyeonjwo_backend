from datetime import datetime, timezone
from app.api.responses import Envelope
from app.core.errors import ServiceError
from app.core.idempotency import request_identity
from app.core.request_context import current_request_id
from app.features.work_cases.application.cursor import utc_time, iso_z
from app.features.work_cases.domain.mail_drafts import DraftAccepted, snapshot_hash
from app.features.work_cases.domain.candidates import selection_staged


class MailDraftService:
    def __init__(self, repo, codec):
        self.repo, self.codec = repo, codec

    def present(self, owner, draft, case):
        if selection_staged(draft):
            raise ServiceError('RESOURCE_NOT_FOUND', '메일 초안을 찾을 수 없습니다.', 404)
        policy = self.repo.send_policy(owner, case, draft['recipient'], draft['purpose'], draft['supplier_id'])
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
                raise ServiceError(code, '최신 초안·설정과 서버 실행 상태를 확인해 주세요.', statuses[code])
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
