"""Read-only hints for extraction HOLD. Mutation RPC remains authoritative."""
from app.features.work_cases.application.cursor import utc_time
from app.features.work_cases.domain.hold import is_hold_output


class ResumeEligibilityRepository:
    def __init__(self, client):
        self.client = client

    def blocks(self, owner, cases):
        if len(cases) > 40:
            result = {}
            for index in range(0, len(cases), 40):
                result.update(self.blocks(owner, cases[index:index + 40]))
            return result
        held = {str(row['id']): row for row in cases}
        if not held:
            return {}
        blocked = {key: ('ACTION_NOT_ALLOWED', '보류된 검토 실행을 확인할 수 없습니다.')
                   for key, row in held.items() if row.get('processing_step') == 'ON_HOLD'}
        try:
            batches = sorted({str(held[key]['current_batch_id']) for key in blocked if held[key].get('current_batch_id')})
            latest, after, scanned = {}, None, 0
            while batches:
                query = (self.client.table('agent_runs')
                         .select('id,owner_user_id,work_case_id,review_batch_id,input_revision,agent_role,status,finished_at,output_jsonb')
                         .eq('owner_user_id', str(owner)).in_('work_case_id', list(blocked))
                         .in_('review_batch_id', batches).eq('component_type', 'AGENT')
                         .eq('status', 'COMPLETED').order('id').limit(200))
                if after:
                    query = query.gt('id', after)
                rows = query.execute().data
                if not isinstance(rows, list):
                    raise ValueError('Invalid rows')
                scanned += len(rows)
                if scanned > 10000:
                    raise ValueError('Scan limit')
                for run in rows:
                    key = str(run.get('work_case_id'))
                    case = held.get(key)
                    if (case is None or str(run.get('owner_user_id')) != str(owner)
                            or str(run.get('review_batch_id')) != str(case.get('current_batch_id'))
                            or run.get('input_revision') != case['input_revision'] or run.get('status') != 'COMPLETED'):
                        continue
                    order = (utc_time(run['finished_at']), run['id'])
                    if key not in latest or order > latest[key][0]:
                        latest[key] = (order, run)
                if len(rows) < 200:
                    break
                new_after = rows[-1]['id']
                if after is not None and new_after <= after:
                    raise ValueError('Unstable pagination')
                after = new_after
            for key, (_, run) in latest.items():
                if run.get('agent_role') == 'INFO_EXTRACTOR' and is_hold_output(run.get('output_jsonb')):
                    blocked.pop(key, None)
            # Any unresolved send blocks extraction restart, including a draft
            # created before the user held the case. Do not work around MW01.
            drafts = (self.client.table('mail_drafts').select('id,replaces_id,work_case_id,owner_user_id,status')
                      .eq('owner_user_id', str(owner)).in_('work_case_id', list(held))
                      .limit(1000).execute().data)
            if not isinstance(drafts, list) or len(drafts) >= 1000:
                raise ValueError('Incomplete send state')
            owned_drafts = [draft for draft in drafts if str(draft.get('owner_user_id')) == str(owner)
                            and str(draft.get('work_case_id')) in held]
            replaced = {str(draft.get('replaces_id')) for draft in owned_drafts if draft.get('replaces_id')}
            for draft in owned_drafts:
                key = str(draft.get('work_case_id'))
                if str(draft.get('id')) in replaced and draft.get('status') == 'DRAFT':
                    continue
                if key in held and str(draft.get('owner_user_id')) == str(owner):
                    if draft.get('status') == 'UNKNOWN':
                        blocked[key] = ('SEND_RESULT_UNKNOWN', '발송 결과를 먼저 확인한 뒤 검토를 재개해 주세요.')
                    elif draft.get('status') in {'DRAFT', 'SENDING'} and blocked.get(key, (None,))[0] != 'SEND_RESULT_UNKNOWN':
                        blocked[key] = ('ACTION_NOT_ALLOWED', '미해결 승인 또는 발송 요청을 먼저 확인해 주세요.')
            return blocked
        except Exception:
            # A read failure must hide the action, not guess that resuming is safe.
            return {key: ('PROCESSING_UNAVAILABLE', '재개 가능 여부를 확인할 수 없습니다. 잠시 후 다시 조회해 주세요.') for key in held}
