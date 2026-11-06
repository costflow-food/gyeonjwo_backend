"""One action policy for the list and detail views; SQL rechecks mutations."""
from app.features.work_cases.application.start_readiness import StartReadiness


def case_actions(case, readiness: StartReadiness, resume_readiness: StartReadiness | None = None,
                 *, case_changes_ready=False):
    actions = ['VIEW_ORIGINAL']
    reason = None
    archived = case.get('archived_at') is not None
    decision = case.get('decision')
    review_ready = case.get('status') == 'Complete' and case.get('processing_step') == 'REVIEW_READY'
    if archived:
        reason = ('CASE_ARCHIVED', '보관된 업무는 먼저 복원해야 합니다.')
    elif (case.get('status') == 'Waiting' and case.get('processing_step') == 'PENDING_START'
          and case.get('decision') is None and case.get('current_batch_id') is None):
        actions.append('ACCEPT_INCREASE')
        if readiness.available:
            actions.insert(1, 'START')
        else:
            reason = (readiness.code, readiness.message)
    elif case.get('status') == 'Waiting' and case.get('decision') == 'ACCEPT_INCREASE':
        reason = ('ACTION_NOT_ALLOWED', '수용 이력이 있는 업무는 명시적으로 재개해야 합니다.')
    elif case.get('current_batch_id') or case.get('status') in {'Running', 'Complete'}:
        reason = ('WORK_ALREADY_STARTED', '이미 검토를 시작한 업무입니다. 현재 진행 상태를 확인해 주세요.')
    else:
        reason = ('ACTION_NOT_ALLOWED', '현재 업무 상태에서는 최초 검토를 시작할 수 없습니다.')
    disabled = ([{'action': 'START', 'reason_code': reason[0], 'message': reason[1]}] if reason else [])
    if case.get('status') == 'Waiting' and case.get('decision') == 'ACCEPT_INCREASE':
        disabled.append({'action': 'RESUME', 'reason_code': 'ACTION_NOT_ALLOWED',
                         'message': '현재 재개 API는 추출 보류 업무의 재개만 지원합니다.'})
    # Only extraction HOLD is supported here; approval/UNKNOWN and archive
    # cannot be bypassed by resuming. The transaction rechecks all guards.
    if case.get('processing_step') == 'ON_HOLD':
        blocked = None
        if case.get('archived_at') is not None:
            blocked = ('CASE_ARCHIVED', '보관된 업무는 먼저 복원해야 합니다.')
        elif case.get('status') != 'Pending' or case.get('decision') is not None or not case.get('current_batch_id'):
            blocked = ('ACTION_NOT_ALLOWED', '현재 상태에서는 추출 보류 업무를 재개할 수 없습니다.')
        else:
            resume = resume_readiness or StartReadiness('RESUME_INTEGRATION_REQUIRED', '보류 업무의 재개 저장 기능이 아직 연결되지 않았습니다.')
            unavailable = readiness if not readiness.available else resume
            if unavailable.available:
                actions.append('RESUME')
            else:
                blocked = (unavailable.code, unavailable.message)
        if blocked:
            disabled.append({'action': 'RESUME', 'reason_code': blocked[0], 'message': blocked[1]})
    can_archive = not archived and case.get('status') in {'Waiting', 'Pending', 'Complete'}
    pending_actions = ['RESTORE'] if archived else []
    if can_archive:
        pending_actions.append('ARCHIVE')
    elif not archived:
        disabled.append({'action': 'ARCHIVE', 'reason_code': 'ACTION_NOT_ALLOWED',
                         'message': '현재 상태에서는 업무를 보관할 수 없습니다.'})
    for action in ('SELECT_PLAN', 'HOLD', 'CONFIRM_REVIEW'):
        if review_ready and not archived and decision not in {'ACCEPT_INCREASE', 'CONFIRM_REVIEW'}:
            pending_actions.append(action)
        else:
            disabled.append({'action': action, 'reason_code': 'CASE_ARCHIVED' if archived else 'ACTION_NOT_ALLOWED',
                             'message': '최신 검토 카드에서 판단할 수 있습니다.'})
    if case_changes_ready:
        actions.extend(pending_actions)
    else:
        for action in pending_actions:
            disabled.append({'action': action, 'reason_code': 'CASE_CHANGE_INTEGRATION_REQUIRED',
                             'message': '업무 변경 기능이 아직 준비되지 않았습니다.'})
    return actions, disabled
