"""Page-batched, owner/revision/batch-scoped hold reads; no writes or new RPCs."""
from uuid import UUID
from app.core.errors import ServiceError
from app.features.work_cases.domain.hold import from_agent_output, hold_info, is_hold_output


class HoldReasonRepository:
    def __init__(self, client):
        self.client = client

    def for_cases(self, owner, cases):
        if not isinstance(cases, (list, tuple)) or len(cases) > 100:
            raise ServiceError('PROCESSING_UNAVAILABLE', '보류 사유 조회 범위를 확인할 수 없습니다.', 503)
        if len(cases) > 40:
            combined = {}
            for index in range(0, len(cases), 40):
                combined.update(self.for_cases(owner, cases[index:index + 40]))
            return combined
        held = {str(UUID(str(row['id']))): row for row in cases if row.get('processing_step') == 'ON_HOLD'}
        result = {case_id: hold_info(None) for case_id in held}
        batches = {str(row['current_batch_id']) for row in held.values() if row.get('current_batch_id')}
        if not batches:
            return result
        try:
            owner = str(UUID(str(owner)))
            # A run batch has bounded role calls. Keyset pagination also handles
            # older data with more runs without relying on a REST row cap.
            after = None
            selected = {}
            scanned = 0
            while True:
                query = (self.client.table('agent_runs')
                    .select('id,owner_user_id,work_case_id,input_revision,review_batch_id,status,output_jsonb,finished_at')
                    .eq('owner_user_id', owner).in_('work_case_id', list(held))
                    .in_('review_batch_id', sorted(batches)).eq('status', 'COMPLETED')
                    .eq('component_type', 'AGENT').order('id').limit(200))
                if after is not None:
                    query = query.gt('id', after)
                rows = query.execute().data
                if not isinstance(rows, list):
                    raise ValueError('Invalid run rows')
                scanned += len(rows)
                if scanned > 10000:
                    raise ValueError('Hold run scan limit')
                for run in rows:
                    case_id = str(run.get('work_case_id'))
                    case = held.get(case_id)
                    if (case is None or str(run.get('owner_user_id')) != owner
                            or run.get('input_revision') != case['input_revision']
                            or str(run.get('review_batch_id')) != str(case.get('current_batch_id'))
                            or run.get('status') != 'COMPLETED'):
                        continue
                    # Choose the latest completed run BEFORE checking HOLD. A
                    # later successful run must not resurrect an earlier hold.
                    from app.features.work_cases.application.cursor import utc_time
                    key = (utc_time(run['finished_at']), str(UUID(run['id'])))
                    if case_id not in selected or key > selected[case_id][0]:
                        selected[case_id] = (key, run['output_jsonb'])
                if len(rows) < 200:
                    break
                next_id = rows[-1]['id']
                if after is not None and next_id <= after:
                    raise ValueError('Unstable hold pagination')
                after = next_id
            for case_id, (_, output) in selected.items():
                if is_hold_output(output):
                    result[case_id] = from_agent_output(output)
            return result
        except ServiceError:
            raise
        except Exception:
            raise ServiceError('PROCESSING_UNAVAILABLE', '보류 사유를 조회할 수 없습니다.', 503) from None
