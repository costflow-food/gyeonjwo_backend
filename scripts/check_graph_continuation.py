"""BE-2 admin-only, read-only verification on the actual API host.

python -m scripts.check_graph_continuation --case-prefix ce6994f4
Uses the server secret client. Never expose this script as a browser API.
Does not schedule jobs or run models: BE-1's single GraphWorker owns continuation.
"""
import argparse
import json
import re
from uuid import UUID
from app.core.config import get_settings
from app.core.errors import ServiceError
from app.shared.infrastructure.db.client import make_server_client
from app.features.work_cases.infrastructure.candidate_repository import CandidateRepository


def inspect_case(client, prefix):
    compact = prefix.replace('-', '').lower()
    if not re.fullmatch(r'[0-9a-f]{8,32}', compact):
        raise ValueError('Use 8 to 32 hexadecimal UUID characters')
    lower, upper = (str(UUID(hex=compact.ljust(32, fill))) for fill in ('0', 'f'))
    rows = (client.table('work_cases').select(
        'id,owner_user_id,input_revision,current_batch_id,status,processing_step,archived_at,decision')
        .gte('id', lower).lte('id', upper).limit(2).execute().data)
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ServiceError('PROCESSING_UNAVAILABLE', '업무 조회 형식을 확인할 수 없습니다.', 503)
    if len(rows) != 1:
        return {'verified': False, 'code': 'RESOURCE_NOT_FOUND' if not rows else 'CASE_PREFIX_AMBIGUOUS'}
    case = rows[0]
    UUID(str(case['id']))
    UUID(str(case['owner_user_id']))
    if type(case['input_revision']) is not int or case['input_revision'] < 1:
        raise ValueError('Invalid revision')
    # The admin lookup resolves one explicit target; subsequent API-equivalent
    # reads always use that stored owner and case, never a browser-supplied owner.
    repo = CandidateRepository(client)
    try:
        candidates = repo.latest(case['owner_user_id'], case['id'])
    except ServiceError as error:
        if error.code != 'CANDIDATES_NOT_READY':
            raise
        candidates = None
    current = bool(candidates is not None and candidates.revision == case['input_revision']
                   and str(candidates.review_batch_id) == str(case.get('current_batch_id')))
    result = {'scope': 'BE2_CANDIDATE_STORAGE_ONLY',
              'verified': current and bool(candidates.items), 'be2_acceptance_verified': False,
              'case_id': str(case['id']),
              'status': case['status'], 'processing_step': case['processing_step'],
              'input_revision': case['input_revision'],
              'candidate_set_id': str(candidates.candidate_set_id) if candidates else None,
              'candidate_count': len(candidates.items) if candidates else 0,
              'current_candidate_set': current, 'writes_performed': False,
              'http_api11_verified': False}
    if result['verified']:
        result['code'] = 'CANDIDATES_STORED'
    elif case.get('archived_at') or case.get('decision') in {'ACCEPT_INCREASE', 'CONFIRM_REVIEW'}:
        result['code'] = 'EXPLICIT_REOPEN_REQUIRED'
    elif case['status'] != 'Running':
        result['code'] = 'EXPLICIT_RESUME_REQUIRED'
    elif case['processing_step'] in {'ON_HOLD', 'FAILED'}:
        result['code'] = 'EXPLICIT_RESUME_REQUIRED'
    elif case['processing_step'] == 'NEEDS_INFO':
        result['code'] = 'REQUIRED_INPUTS_MISSING'
    elif case['processing_step'] in {'WAITING_APPROVAL', 'AWAITING_APPROVAL', 'SEND_UNKNOWN'}:
        result['code'] = 'RESOLVE_SEND_STATE_REQUIRED'
    else:
        result['code'] = 'CHECK_GRAPH_JOB_AND_SUPPLIER_EVIDENCE'
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--case-prefix', required=True)
    args = parser.parse_args()
    try:
        result = inspect_case(make_server_client(get_settings()), args.case_prefix)
    except Exception:
        result = {'verified': False, 'code': 'CHECK_FAILED', 'writes_performed': False}
    # No supplier names, contact data, email/model bodies or provider errors.
    print(json.dumps(result, ensure_ascii=True, indent=2))
    return 0 if result['verified'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
