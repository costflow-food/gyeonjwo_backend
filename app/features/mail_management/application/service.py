"""Read-only presentation plus durable mutation boundaries; never calls an LLM/Gmail."""
from datetime import datetime, timezone
from uuid import UUID
from app.api.responses import Envelope
from app.core.errors import ServiceError
from app.core.idempotency import request_identity
from app.core.request_context import current_request_id
from app.features.work_cases.application.cursor import iso_z, utc_time
from app.features.work_cases.domain.rfq import mailbox_address
from app.features.mail_management.domain.models import (
    PURPOSES, MAX_VERSION, IntakePage, IntakeItem, IntakeCandidate, ResolvedMail, SendSettings,
    snapshot_version, mail_snapshot)


def pending(row):
    return (row.get('work_case_id') is None and row.get('processing_status') == 'NEEDS_REVIEW'
            and (row.get('mail_classification') == 'UNCERTAIN' or row.get('link_status') == 'AMBIGUOUS'))


def settings_snapshot(base, override):
    return {'mailbox_settings_id': base['id'] if base else None,
            'case_settings_id': override['id'] if override else None}


def settings_version(base, override):
    # Injective integer pairing of the two immutable source version counters.
    # Unlike adding counters, (base=2,case=1) cannot collide with (1,2).
    a, b = (base['version_no'] if base else 0), (override['version_no'] if override else 0)
    if (any(type(value) is not int or value < 0 for value in (a, b))
            or (base is not None and a == 0) or (override is not None and b == 0)):
        raise ServiceError('PROCESSING_UNAVAILABLE', '설정 버전을 확인할 수 없습니다.', 503)
    value = (a + b) * (a + b + 1) // 2 + b
    if value > MAX_VERSION:
        raise ServiceError('PROCESSING_UNAVAILABLE', '설정 버전 범위를 확인할 수 없습니다.', 503)
    return value


def purpose_setting(base, override, purpose):
    own_row = override
    own = (own_row or {}).get('approval_required_by_purpose', {}).get(purpose)
    base_value = (base or {}).get('approval_required_by_purpose', {}).get(purpose)
    chosen = override if type(own) is bool else (base if type(base_value) is bool else None)
    effective = chosen['approval_required_by_purpose'][purpose] if chosen else None
    return own if type(own) is bool else None, chosen, effective


class MailManagementService:
    def __init__(self, repo, codec, now=None):
        self.repo, self.codec = repo, codec
        self.now = now or (lambda: datetime.now(timezone.utc))

    def intake(self, owner, *, cursor=None, limit=25):
        scope = self.codec.scope(owner, 'MAIL_INTAKE', False, '')
        snapshot, after = self.codec.decode(cursor, scope) if cursor else (iso_z(self.now()), None)
        rows = self.repo.pending(owner, snapshot, after, limit + 1)
        page = rows[:limit]
        items = []
        try:
            for row in page:
                if str(row['owner_user_id']) != str(owner) or not pending(row):
                    raise ValueError('Invalid intake scope')
                ambiguous = row['link_status'] == 'AMBIGUOUS'
                candidates = []
                if ambiguous:
                    for case in self.repo.candidates(owner, row):
                        initial = self.repo.mail(owner, case['initial_mail_id'])
                        candidates.append(IntakeCandidate(case_id=case['id'],
                            title=case.get('requester_name') or initial['subject'] or initial['sender'],
                            status=case['status'], item_name=case.get('item_name'),
                            reply_evidence='답장 헤더가 기존 발송 기록을 참조합니다.'))
                attachments = row.get('attachments_jsonb') or []
                if not isinstance(attachments, list):
                    raise ValueError('Invalid attachment metadata')
                reason = row.get('classification_reason')
                reason = ' '.join(reason.split())[:300] if isinstance(reason, str) else ''
                items.append(IntakeItem(mail_id=row['id'], subject=row['subject'], sender=row['sender'],
                    received_at=iso_z(utc_time(row['sent_or_received_at'])), body_text=row['body_text'],
                    # Frontend's intake reason enum uses AMBIGUOUS here. The
                    # original API-25 still exposes the stored classification.
                    mail_classification='AMBIGUOUS' if ambiguous else 'UNCERTAIN',
                    reason_detail=('기존 업무와의 연결을 확인해 주세요.' if ambiguous else
                                   reason or '업무 요청인지 확인해 주세요.'),
                    attachments=[a['filename'] for a in attachments if isinstance(a, dict) and isinstance(a.get('filename'), str)],
                    candidates=candidates, revision=snapshot_version(mail_snapshot(row))))
            next_cursor = self.codec.encode(scope, snapshot,
                (iso_z(utc_time(page[-1]['sent_or_received_at'])), str(UUID(page[-1]['id'])))) if len(rows) > limit else None
            return IntakePage(items=items, next_cursor=next_cursor, total_pending=self.repo.total_pending(owner))
        except (ValueError, TypeError, KeyError):
            raise ServiceError('PROCESSING_UNAVAILABLE', '접수 확인 목록을 표시할 수 없습니다.', 503) from None

    def present_settings(self, owner, mailbox_ref=None, case_id=None):
        mailbox_ref, case = self.repo.scope(owner, mailbox_ref, case_id)
        base, override = self.repo.settings(owner, mailbox_ref, case_id)
        suppliers = self.repo.suppliers(owner)
        drafts = self.repo.drafts(owner, case_id)
        return self.settings_dto(mailbox_ref, case_id, base, override, suppliers, drafts)

    def settings_dto(self, mailbox_ref, case_id, base, override, suppliers, drafts):
        try:
            return self._settings_dto(mailbox_ref, case_id, base, override, suppliers, drafts)
        except ServiceError:
            raise
        except (ValueError, TypeError, KeyError, AttributeError):
            raise ServiceError('PROCESSING_UNAVAILABLE', '저장된 발송 설정을 표시할 수 없습니다.', 503) from None

    def _settings_dto(self, mailbox_ref, case_id, base, override, suppliers, drafts):
        effective, missing = {}, []
        labels = {'RFQ': '견적 요청', 'CLARIFICATION': '추가 확인', 'COUNTER_OFFER': '역제안'}
        for purpose in PURPOSES:
            own, chosen, value = purpose_setting(base, override if case_id else base, purpose)
            effective[purpose] = {'approval_required': own, 'effective_approval_required': value,
                'source': ('CASE' if case_id and chosen is override else 'MAILBOX') if chosen else None,
                'source_version': chosen['version_no'] if chosen else None,
                'source_settings_id': chosen['id'] if chosen else None}
            if chosen is None:
                missing.append(labels[purpose] + ' 승인 여부')
            elif chosen.get('configured') is not True:
                missing.append(labels[purpose] + ' 발송 범위 확인')
        # Every registered supplier with a contact address can receive mail; there is no allow list.
        allowed = [{'supplier_id': row['id'], 'name': row['name'], 'email': row.get('contact_email'),
                    'allowed': bool(row.get('contact_email'))} for row in suppliers]
        style = {**((base or {}).get('style_jsonb') or {}), **((override or {}).get('style_jsonb') or {})}
        names = {row['id']: row['name'] for row in suppliers}
        try:
            return SendSettings(mailbox_ref=mailbox_ref, case_id=case_id, configured=not missing,
                settings_version=settings_version(base, override), missing_items=missing,
                effective_settings=effective, allowed_suppliers=allowed,
                style={'tone': style.get('tone', 'FORMAL'), 'signature': style.get('signature', '')},
                pending_drafts=[{'draft_id': row['id'], 'purpose': row['purpose'],
                    'supplier_name': names.get(row['supplier_id'], ''), 'recipient': row['recipient'],
                    'status': row['status']} for row in drafts])
        except (ValueError, TypeError, KeyError):
            raise ServiceError('PROCESSING_UNAVAILABLE', '저장된 발송 설정을 표시할 수 없습니다.', 503) from None

    def saved(self, result, model):
        try:
            if result['kind'] not in {'ACCEPTED', 'REPLAY'} or result['http_status'] != 200:
                raise ValueError('Invalid mutation response')
            response = Envelope[model].model_validate(result['response']).model_dump(mode='json')
            if not response['meta']['request_id']:
                raise ValueError('Missing request ID')
            return response
        except (ValueError, KeyError, TypeError):
            raise ServiceError('PROCESSING_UNAVAILABLE', '저장된 요청 결과를 확인할 수 없습니다.', 503) from None

    def resolve(self, owner, mail_id, payload, key):
        body = payload.model_dump(mode='json')
        identity = request_identity(str(owner), 'POST', f'/api/v1/mail-messages/{mail_id}/resolve', key, body)
        # Read-only lookup before state/revision checks preserves successful replay.
        result = self.repo.receipt(identity, 'INTAKE_INTEGRATION_REQUIRED')
        if result is None:
            row = self.repo.mail(owner, mail_id)
            if not pending(row):
                raise ServiceError('REVISION_CONFLICT', '이미 처리된 메일입니다. 목록을 다시 확인해 주세요.', 409)
            snapshot = mail_snapshot(row)
            if payload.expected_revision is not None and payload.expected_revision != snapshot_version(snapshot):
                raise ServiceError('REVISION_CONFLICT', '메일 정보가 바뀌었습니다. 최신 내용을 확인해 주세요.', 409)
            target = None
            if payload.target_case_id:
                _, target = self.repo.scope(owner, row['mailbox_ref'], payload.target_case_id)
                if target.get('archived_at'):
                    raise ServiceError('INVALID_LINK_TARGET', '보관된 업무에는 메일을 연결할 수 없습니다.', 422)
            result = self.repo.resolve(owner, mail_id, body, {'mail': snapshot, 'target_case': target}, identity, current_request_id())
        response = self.saved(result, ResolvedMail)
        data = response['data']
        if (data['mail_id'] != str(mail_id) or data['resolution'] != payload.resolution
                or (payload.target_case_id and data['case_id'] != str(payload.target_case_id))):
            raise ServiceError('PROCESSING_UNAVAILABLE', '접수 판단 결과의 범위를 확인할 수 없습니다.', 503)
        return response

    def save_settings(self, owner, payload, key, *, dispatch_ready=False):
        body = payload.model_dump(mode='json')
        identity = request_identity(str(owner), 'PUT', '/api/v1/mail-send-settings', key, body)
        if payload.apply_to == 'SPECIFIED_DRAFTS' and (payload.case_id is None or not payload.draft_ids):
            raise ServiceError('SEND_SCOPE_REQUIRED', '적용할 업무와 초안을 선택해 주세요.', 422,
                               fields=['body.case_id', 'body.draft_ids'])
        if payload.apply_to == 'FUTURE_DRAFTS' and payload.draft_ids:
            raise ServiceError('SEND_SCOPE_REQUIRED', '새 초안에만 적용할 때는 기존 초안을 선택할 수 없습니다.', 422,
                               fields=['body.draft_ids'])
        result = self.repo.receipt(identity, 'SETTINGS_INTEGRATION_REQUIRED')
        if result is None:
            mailbox_ref, case = self.repo.scope(owner, payload.mailbox_ref, payload.case_id)
            base, override = self.repo.settings(owner, mailbox_ref, payload.case_id)
            if payload.expected_settings_version != settings_version(base, override):
                raise ServiceError('SETTINGS_CHANGED', '설정이 바뀌었습니다. 최신 설정을 확인해 주세요.', 409)
            if case and case.get('archived_at'):
                raise ServiceError('ACTION_NOT_ALLOWED', '보관된 업무의 발송 설정은 변경할 수 없습니다.', 409)
            suppliers = {row['id']: row for row in self.repo.suppliers(owner)}
            drafts = {row['id']: row for row in self.repo.drafts(owner, payload.case_id)}
            selected = []
            for draft_id in payload.draft_ids:
                row = drafts.get(str(draft_id))
                if row is None or row['status'] != 'DRAFT' or (case and row['input_revision'] != case['input_revision']):
                    raise ServiceError('DRAFT_CHANGED', '초안 상태가 바뀌었습니다. 다시 확인해 주세요.', 409)
                try:
                    recipient = mailbox_address(row['recipient'])
                except (ValueError, TypeError):
                    raise ServiceError('SEND_SCOPE_REQUIRED', '초안의 수신 주소를 확인해 주세요.', 422) from None
                try:
                    contact = mailbox_address(suppliers[row['supplier_id']]['contact_email'])
                except (ValueError, KeyError, TypeError):
                    contact = None
                if recipient != contact:
                    raise ServiceError('SEND_SCOPE_REQUIRED', '초안의 수신 대상이 등록 업체 주소와 다릅니다.', 422)
                selected.append(row)
            if payload.apply_to == 'SPECIFIED_DRAFTS' and not dispatch_ready:
                raise ServiceError('PROCESSING_UNAVAILABLE', '기존 초안을 처리할 발송 워커가 준비되지 않았습니다.', 503)
            # Immutable IDs identify the combined policy. DB also locks the case
            # and selected draft snapshots before persisting settings/jobs.
            snapshot = {**settings_snapshot(base, override), 'case': case, 'drafts': selected,
                        'dispatch_ready': bool(dispatch_ready)}
            result = self.repo.save_settings(owner, body, snapshot, identity, current_request_id())
        response = self.saved(result, SendSettings)
        if response['data']['mailbox_ref'] != payload.mailbox_ref or response['data']['case_id'] != (str(payload.case_id) if payload.case_id else None):
            raise ServiceError('PROCESSING_UNAVAILABLE', '발송 설정 결과의 범위를 확인할 수 없습니다.', 503)
        return response
