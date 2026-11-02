"""One action policy for the list and detail views; SQL rechecks mutations."""
from app.features.work_cases.application.start_readiness import StartReadiness


def case_actions(case, readiness: StartReadiness):
    actions = ['VIEW_ORIGINAL']
    reason = None
    if case.get('archived_at') is not None:
        actions.append('RESTORE')
        reason = ('CASE_ARCHIVED', '보관된 업무는 먼저 복원해야 합니다.')
    elif (case.get('status') == 'Waiting' and case.get('processing_step') == 'PENDING_START'
          and case.get('decision') is None and case.get('current_batch_id') is None):
        actions.append('ACCEPT_INCREASE')
        if readiness.available:
            actions.insert(1, 'START')
        else:
            return actions, [readiness.disabled_action()]
    elif case.get('status') == 'Waiting' and case.get('decision') == 'ACCEPT_INCREASE':
        actions.append('RESUME')
        reason = ('ACTION_NOT_ALLOWED', '수용 이력이 있는 업무는 명시적으로 재개해야 합니다.')
    elif case.get('current_batch_id') or case.get('status') in {'Running', 'Complete'}:
        reason = ('WORK_ALREADY_STARTED', '이미 검토를 시작한 업무입니다. 현재 진행 상태를 확인해 주세요.')
    else:
        reason = ('ACTION_NOT_ALLOWED', '현재 업무 상태에서는 최초 검토를 시작할 수 없습니다.')
    return actions, ([{'action': 'START', 'reason_code': reason[0], 'message': reason[1]}] if reason else [])
