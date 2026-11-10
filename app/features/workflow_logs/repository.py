"""Read workflow logs through explicit owner predicates and a public field allowlist."""
from datetime import datetime, timezone
from uuid import UUID

from app.core.errors import ServiceError
from app.features.work_cases.application.cursor import iso_z, utc_time


TEXT_FIELDS = (
    'component_type', 'agent_role', 'summary', 'outcome', 'request_id', 'run_status',
    'send_policy', 'tool_name', 'tool_result_ref', 'error_code', 'mail_id', 'draft_id',
)
VERSION_FIELDS = ('quote_version', 'settings_version')
# DB가 실제로 남기는 값. 로그 payload의 키는 role·status이고, 주체 종류는 actor_type 컬럼에 있다.
COMPONENT_TYPES = frozenset({'AGENT', 'NODE', 'TOOL', 'USER', 'SYSTEM'})
RUN_OUTCOMES = {'COMPLETED': 'SUCCESS', 'FAILED': 'FAILURE', 'HOLD': 'WAITING'}


def public_log(row, titles):
    item = {
        'id': str(UUID(row['id'])),
        'created_at': iso_z(utc_time(row['created_at'])),
        'event_type': row['event_type'],
    }
    if not isinstance(item['event_type'], str) or not item['event_type']:
        raise ValueError('Invalid workflow event type')
    payload = row.get('payload_jsonb')
    if not isinstance(payload, dict):
        payload = {}
    case_id = row.get('work_case_id')
    if case_id is not None:
        case_id = str(UUID(case_id))
        item['case_id'] = case_id
        title = payload.get('case_title')
        if not isinstance(title, str) or not title.strip():
            title = titles.get(case_id)
        if isinstance(title, str) and title.strip():
            item['case_title'] = title
    for key in TEXT_FIELDS:
        if isinstance(payload.get(key), str):
            item[key] = payload[key]
    # 그래프 실행 로그는 payload에 agent_role·run_status 대신 role·status를 남긴다.
    # 응답 계약(component_type·agent_role·run_status·outcome)은 그대로 두고 여기서 채운다.
    if 'component_type' not in item and row.get('actor_type') in COMPONENT_TYPES:
        item['component_type'] = row['actor_type']
    if 'agent_role' not in item and isinstance(payload.get('role'), str):
        item['agent_role'] = payload['role']
    status = payload.get('status')
    if 'run_status' not in item and isinstance(status, str):
        item['run_status'] = status
    if 'outcome' not in item:
        if item['event_type'] == 'GRAPH_FAILED':
            item['outcome'] = 'FAILURE'
        elif item['event_type'] == 'GRAPH_ROLE_FINISHED' and status in RUN_OUTCOMES:
            item['outcome'] = RUN_OUTCOMES[status]
    for key in VERSION_FIELDS:
        if type(payload.get(key)) in (int, str):
            item[key] = payload[key]
    if type(payload.get('duration_ms')) is int and payload['duration_ms'] >= 0:
        item['duration_ms'] = payload['duration_ms']
    refs = payload.get('evidence_refs')
    if isinstance(refs, list) and all(isinstance(ref, str) for ref in refs):
        item['evidence_refs'] = refs
    return item


def list_workflow_logs(client, codec, owner, filters):
    case_id = str(filters.case_id) if filters.case_id else None
    scope = codec.scope(owner, 'workflow-logs', None, case_id)
    snapshot, after = (codec.decode(filters.cursor, scope) if filters.cursor is not None
                       else (iso_z(datetime.now(timezone.utc)), None))

    def filtered(query):
        query = query.eq('owner_user_id', str(owner)).lte('created_at', snapshot)
        return query.eq('work_case_id', case_id) if case_id else query

    try:
        total = filtered(client.table('workflow_logs').select('id', count='exact', head=True)).execute().count
        query = filtered(client.table('workflow_logs').select(
            'id,created_at,event_type,work_case_id,actor_type,payload_jsonb'))
        if after:
            timestamp, log_id = after
            query = query.or_(f'created_at.lt.{timestamp},and(created_at.eq.{timestamp},id.lt.{log_id})')
        rows = query.order('created_at', desc=True).order('id', desc=True).limit(filters.limit + 1).execute().data
        if type(total) is not int or total < 0 or not isinstance(rows, list):
            raise ValueError('Invalid workflow log results')
        page = rows[:filters.limit]
        ids = sorted({row['work_case_id'] for row in page if row.get('work_case_id')})
        titles = {}
        if ids:
            cases = (client.table('work_cases').select('id,item_name').eq('owner_user_id', str(owner))
                     .in_('id', ids).execute().data)
            titles = {row['id']: row.get('item_name') for row in cases}
        items = [public_log(row, titles) for row in page]
        next_cursor = (codec.encode(scope, snapshot, (items[-1]['created_at'], items[-1]['id']))
                       if len(rows) > filters.limit else None)
        return {'items': items, 'next_cursor': next_cursor, 'counts': {'total': total}}
    except Exception:
        raise ServiceError('PROCESSING_UNAVAILABLE', '업무 로그를 조회할 수 없습니다.', 503) from None
