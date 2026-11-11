from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime, timezone
from threading import Lock
from uuid import UUID

from app.core.errors import ServiceError
from app.features.work_cases.application.cursor import CursorCodec, iso_z, utc_time
from app.features.work_cases.domain.listing import QUEUE_TABS, QueueCounts, WorkCaseList, WorkCaseRow

from app.features.work_cases.application.case_actions import case_actions
from app.features.work_cases.application.start_readiness import StartReadiness
from app.shared.domain.item_names import contains_ignoring_spaces

QUEUE_TAB_NAMES = frozenset(QUEUE_TABS.values())

_PAGE_READS = ThreadPoolExecutor(max_workers=8, thread_name_prefix='case-page-read')

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
    'SELECTING_CANDIDATES': '견적 요청 후보 찾는 중',
    'CANDIDATES': '견적 요청 업체 선택 대기',
    'GENERATING_PLANS': '검토안 생성 중',
    'BUILDING_PLANS': '구매 대응안 생성 중',
    'BUILDING_COUNTER_OFFER': '역제안 생성 중',
    'SEND_UNKNOWN': '발송 결과 확인 필요',
}


class WorkCaseListService:
    def __init__(self, repository, codec: CursorCodec, now=None, *, extraction_enabled=False):
        self.repo, self.codec = repository, codec
        self.extraction_enabled = extraction_enabled
        self.now = now or (lambda: datetime.now(timezone.utc))
        self._in_flight = {}
        self._in_flight_lock = Lock()

    def list(self, owner, *, status=None, archived=False, q='', limit=25, cursor=None, start_readiness=None, resume_readiness=None,
             case_changes_ready=False, action_readiness=None):
        readiness = start_readiness if start_readiness is not None else (StartReadiness()
            if self.extraction_enabled else StartReadiness('EXTRACTION_DISABLED', '서버의 검토 실행 설정이 꺼져 있습니다.'))
        query = q.strip().casefold()
        scope = self.codec.scope(owner, status, archived, query)
        snapshot, after = (self.codec.decode(cursor, scope) if cursor else (iso_z(self.now()), None))
        # Share only overlapping reads with identical owner, filters and page.
        # Completed results are discarded, so a refresh can see later changes.
        key = (scope, limit, cursor, readiness, resume_readiness, case_changes_ready, action_readiness is not None)
        with self._in_flight_lock:
            pending = self._in_flight.get(key)
            if pending is None:
                pending = self._in_flight[key] = Future()
                leader = True
            else:
                leader = False
        if not leader:
            return pending.result()
        try:
            result = self._read_page(owner, status, archived, query, limit, scope, snapshot, after,
                                     readiness, resume_readiness, case_changes_ready, action_readiness)
        except BaseException as error:
            with self._in_flight_lock:
                del self._in_flight[key]
                pending.set_exception(error)
            raise
        else:
            with self._in_flight_lock:
                del self._in_flight[key]
                pending.set_result(result)
            return result

    def _read_page(self, owner, status, archived, query, limit, scope, snapshot, after,
                   readiness, resume_readiness, case_changes_ready, action_readiness):
        counts, filtered = QueueCounts(), []
        page_cases = {}
        try:
            pending_read = _PAGE_READS.submit(self.repo.intake_pending, owner)
            rows = self.repo.rows(owner, snapshot, archived)
            for case, mail in rows:
                if (case['archived_at'] is not None) != archived:
                    continue
                if utc_time(case['created_at']) > utc_time(snapshot):
                    continue
                requester = (case.get('registered_requester_name') or case.get('requester_name')) if case['processing_step'] != 'PENDING_START' else None
                searchable = [mail.get('subject'), mail.get('sender'), mail.get('sender_name'), case.get('item_name'), requester]
                # 띄어쓰기 무시: '냉동망고'로 검색해도 '냉동 망고' 업무가 나온다
                if query and not any(contains_ignoring_spaces(value, query) for value in searchable):
                    continue
                counts.total += 1
                tab = QUEUE_TABS.get(case['status'])
                if tab is not None:
                    setattr(counts, tab, getattr(counts, tab) + 1)
                # 탭(Waiting·Running·Complete)은 그 탭에 묶인 상태 전체, Pending·Confirm은 그 상태만
                if status is not None and (tab != status if status in QUEUE_TAB_NAMES else case['status'] != status):
                    continue
                received = iso_z(utc_time(mail['sent_or_received_at']))
                key = (received, str(UUID(case['id'])))
                if after is not None and key >= after:
                    continue
                archived_at = iso_z(utc_time(case['archived_at'])) if case['archived_at'] else None
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
                    allowed_actions=[])
                page_cases[str(row.case_id)] = case
                filtered.append((key, row))
            filtered.sort(key=lambda item: item[0], reverse=True)
            page = filtered[:limit]
            # An explicitly absent current round proves 0/0. Older schemas omit
            # the column; those rows still use the authoritative reply-facts RPC.
            reply_case_ids = [str(row.case_id) for _, row in page
                              if page_cases[str(row.case_id)].get('current_rfq_round_id', True) is not None]
            summaries_read = (_PAGE_READS.submit(self.repo.reply_summaries, owner, reply_case_ids)
                              if reply_case_ids else None)
            held = [page_cases[str(row.case_id)] for _, row in page if row.processing_step == 'ON_HOLD']
            hold_read = _PAGE_READS.submit(self.repo.hold_reasons, owner, held) if held else None
            candidate_cases = [page_cases[str(row.case_id)] for _, row in page
                               if row.processing_step in {'CANDIDATES', 'SELECTING_CANDIDATES'}]
            candidate_read = _PAGE_READS.submit(self.repo.candidate_blocks, owner, candidate_cases) if candidate_cases else None
            if page and action_readiness is not None:
                readiness, resume_readiness, case_changes_ready = action_readiness()
            for _, row in page:
                row.allowed_actions, row.disabled_actions = case_actions(
                    page_cases[str(row.case_id)], readiness, resume_readiness, case_changes_ready=case_changes_ready)
            resumable = [page_cases[str(row.case_id)] for _, row in page if 'RESUME' in row.allowed_actions]
            resume_blocks = self.repo.resume_blocks(owner, resumable) if resumable else {}
            summaries = summaries_read.result() if summaries_read else {}
            hold_reasons = hold_read.result() if hold_read else {}
            candidate_blocks = candidate_read.result() if candidate_read else {}
            proceeding = [page_cases[str(row.case_id)] for _, row in page if 'PROCEED' in row.allowed_actions]
            proceed_blocks = self.repo.proceed_blocks(owner, proceeding, summaries) if proceeding else {}
            for _, row in page:
                row.hold = hold_reasons.get(str(row.case_id))
                if str(row.case_id) in resume_blocks:
                    row.allowed_actions.remove('RESUME')
                    code, message = resume_blocks[str(row.case_id)]
                    row.disabled_actions.append({'action': 'RESUME', 'reason_code': code, 'message': message})
                if str(row.case_id) in proceed_blocks:
                    row.allowed_actions.remove('PROCEED')
                    code, message = proceed_blocks[str(row.case_id)]
                    row.disabled_actions.append({'action': 'PROCEED', 'reason_code': code, 'message': message})
                if page_cases[str(row.case_id)].get('current_rfq_round_id', True) is not None:
                    row.reply_summary = summaries[str(row.case_id)]
                if str(row.case_id) in candidate_blocks:
                    from app.features.work_cases.application.candidates import apply_candidate_action
                    apply_candidate_action(page_cases[str(row.case_id)], row.allowed_actions, row.disabled_actions,
                                           candidate_blocks[str(row.case_id)])
            next_cursor = (self.codec.encode(scope, snapshot, page[-1][0]) if len(filtered) > limit else None)
            return WorkCaseList(items=[row for _, row in page], next_cursor=next_cursor,
                                counts=counts, intake_pending=pending_read.result())
        except ServiceError:
            raise
        except Exception:
            raise ServiceError('PROCESSING_UNAVAILABLE', '업무 목록 응답을 확인할 수 없습니다.', 503) from None
