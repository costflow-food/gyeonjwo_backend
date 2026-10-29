from datetime import datetime, timezone
from uuid import UUID

from app.core.errors import ServiceError
from app.features.work_cases.application.cursor import CursorCodec, iso_z, utc_time
from app.features.work_cases.domain.listing import QueueCounts, WorkCaseList, WorkCaseRow

STEP_MESSAGES = {
    'PENDING_START': '원문 확인 후 검토 시작',
    'ACKNOWLEDGED': '수신 메일 확인됨',
    'EXTRACTING': '요청 정보 확인 중',
    'WAITING_REPLY': '거래처 회신 대기',
    'WAITING_APPROVAL': '발송 승인 대기',
    'AWAITING_APPROVAL': '발송 승인 대기',
    'REVIEW_READY': '검토 결과 준비 완료',
    'FAILED': '처리 오류 확인 필요',
    'ON_HOLD': '검토 보류',
    'NEEDS_INFO': '추가 정보 확인 필요',
    'SELECTING_CANDIDATES': '후보 찾기 대기',
}


class WorkCaseListService:
    def __init__(self, repository, codec: CursorCodec, now=None, *, extraction_enabled=False):
        self.repo, self.codec = repository, codec
        self.extraction_enabled = extraction_enabled
        self.now = now or (lambda: datetime.now(timezone.utc))

    def list(self, owner, *, status=None, archived=False, q='', limit=25, cursor=None):
        query = q.strip().casefold()
        scope = self.codec.scope(owner, status, archived, query)
        snapshot, after = (self.codec.decode(cursor, scope) if cursor else (iso_z(self.now()), None))
        counts, filtered = QueueCounts(), []
        try:
            for case, mail in self.repo.rows(owner, snapshot, archived):
                # Defensive checks also support alternative repositories without
                # accidentally applying status to tab counts.
                if (case['archived_at'] is not None) != archived:
                    continue
                if utc_time(case['created_at']) > utc_time(snapshot):
                    continue
                requester = (case.get('registered_requester_name') or case.get('requester_name')) if case['processing_step'] != 'PENDING_START' else None
                searchable = [mail.get('subject'), mail.get('sender'), mail.get('sender_name'), case.get('item_name'), requester]
                if query and not any(query in (value or '').casefold() for value in searchable):
                    continue
                counts.total += 1
                if case['status'] in {'Waiting', 'Running', 'Complete'}:
                    setattr(counts, case['status'], getattr(counts, case['status']) + 1)
                if status is not None and case['status'] != status:
                    continue
                received = iso_z(utc_time(mail['sent_or_received_at']))
                key = (received, str(UUID(case['id'])))
                if after is not None and key >= after:
                    continue
                archived_at = iso_z(utc_time(case['archived_at'])) if case['archived_at'] else None
                actions = ['VIEW_ORIGINAL']
                if archived:
                    actions.append('RESTORE')
                elif case['status'] == 'Waiting' and case['decision'] is None:
                    actions.append('ACCEPT_INCREASE')
                    if self.extraction_enabled:
                        actions.insert(1, 'START')
                elif case['status'] == 'Waiting' and case['decision'] == 'ACCEPT_INCREASE':
                    actions.append('RESUME')
                step = case['processing_step']
                attachments = mail.get('attachments_jsonb') or []
                if not isinstance(attachments, list):
                    raise ValueError('Invalid attachment metadata')
                row = WorkCaseRow(case_id=case['id'], initial_mail_id=case['initial_mail_id'],
                    title=requester or mail['sender'], sender=mail['sender'], sender_name=mail.get('sender_name'),
                    requester_name=case.get('requester_name'),
                    received_at=received, status=case['status'], processing_step=step,
                    input_revision=case['input_revision'], archived_at=archived_at, item_name=case['item_name'],
                    attachment_count=len(attachments), step_detail={'code': step, 'message': STEP_MESSAGES.get(step, '처리 상태 확인')},
                    allowed_actions=actions)
                filtered.append((key, row))
            filtered.sort(key=lambda item: item[0], reverse=True)
            page = filtered[:limit]
            next_cursor = (self.codec.encode(scope, snapshot, page[-1][0]) if len(filtered) > limit else None)
            return WorkCaseList(items=[row for _, row in page], next_cursor=next_cursor,
                                counts=counts, intake_pending=self.repo.intake_pending(owner))
        except ServiceError:
            raise
        except Exception:
            raise ServiceError('PROCESSING_UNAVAILABLE', '업무 목록 응답을 확인할 수 없습니다.', 503) from None
