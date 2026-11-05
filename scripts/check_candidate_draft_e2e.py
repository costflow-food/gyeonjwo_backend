"""BE-3 on the actual Mac API host. No send/approve/settings mutation calls.

Read-only inventory:
 python -m scripts.check_candidate_draft_e2e --case-prefix ce6994f4
Select explicit supplier UUIDs after inventory, using the same login token:
 python -m scripts.check_candidate_draft_e2e --case-prefix ce6994f4 --supplier-id UUID
Token is requested with getpass, never passed in arguments or printed.
The only possible write is the authenticated API-12 selection transaction.
"""
import argparse
import getpass
import hashlib
import json
import re
from uuid import UUID
import httpx
from app.core.config import get_settings
from app.features.mailboxes.application.auth import google_principal
from app.features.work_cases.domain.candidates import CandidatesResponse, CandidateSelectionResponse
from app.features.work_cases.domain.rfq import mailbox_address
from app.shared.infrastructure.db.client import make_server_client


class CheckFailed(Exception):
    pass


def require(condition, code):
    if not condition:
        raise CheckFailed(code)


def data(http, path, **params):
    response = http.get(path, params=params)
    require(response.status_code == 200, 'API_READ_FAILED')
    body = response.json()
    require(isinstance(body, dict) and isinstance(body.get('data'), dict)
            and isinstance(body.get('meta'), dict) and body['meta'].get('request_id'), 'INVALID_API_ENVELOPE')
    return body['data']


def verify(http, db, owner, prefix, selected, *, expected_items=12):
    compact = prefix.replace('-', '').lower()
    require(bool(re.fullmatch('[0-9a-f]{8,32}', compact)), 'INVALID_CASE_PREFIX')
    lower, upper = (str(UUID(hex=compact.ljust(32, fill))) for fill in ('0', 'f'))
    cases = (db.table('work_cases').select('id,standardized_item_key,input_revision')
             .eq('owner_user_id', str(owner)).gte('id', lower).lte('id', upper).limit(2).execute().data)
    require(isinstance(cases, list) and len(cases) == 1, 'CASE_NOT_UNIQUE_OR_NOT_FOUND')
    case = cases[0]
    require(isinstance(case.get('standardized_item_key'), str) and case['standardized_item_key'], 'ITEM_KEY_REQUIRED')
    candidate = CandidatesResponse.model_validate(data(http, f"/api/v1/work-cases/{case['id']}/candidates"))
    require(str(candidate.case_id) == case['id'] and candidate.revision == case['input_revision']
            and not candidate.is_stale, 'STALE_CANDIDATES')
    items = (db.table('supplier_items').select('id,supplier_id').eq('owner_user_id', str(owner))
             .eq('standardized_item_key', case['standardized_item_key']).order('id').limit(expected_items + 1).execute().data)
    require(isinstance(items, list), 'INVALID_ITEM_ROWS')
    supplier_ids = sorted({str(UUID(row['supplier_id'])) for row in items})
    suppliers = (db.table('suppliers').select('id,contact_email').eq('owner_user_id', str(owner))
                 .in_('id', supplier_ids).execute().data) if supplier_ids else []
    contacts = {}
    for supplier in suppliers:
        try:
            mailbox_address(supplier.get('contact_email'))
            contacts[supplier['id']] = True
        except ValueError:
            contacts[supplier['id']] = False
    missing = [sid for sid in supplier_ids if not contacts.get(sid)]
    report = {'scope': 'BE3', 'case_id': case['id'], 'revision': candidate.revision,
              'candidate_set_id': str(candidate.candidate_set_id), 'supplier_item_count': len(items),
              'expected_item_count': expected_items, 'supplier_count': len(supplier_ids),
              'missing_email_supplier_ids': missing,
              'selectable_supplier_ids': [str(item.supplier_id) for item in candidate.items if item.selectable],
              'selection_performed': False, 'verified': False, 'send_api_called': False}
    if not selected:
        return report
    require(len(items) == expected_items and not missing, 'ITEM_INVENTORY_OR_EMAILS_INCOMPLETE')
    selected = sorted({str(UUID(sid)) for sid in selected})
    require(set(selected) <= set(report['selectable_supplier_ids']), 'INVALID_TARGET_SELECTION')
    require(set(selected) <= set(supplier_ids), 'TARGET_OUTSIDE_ITEM_INVENTORY')
    settings = data(http, '/api/v1/mail-send-settings', case_id=case['id'])
    # Approval ON keeps the selection from sending; there is no server-wide send switch.
    require(settings.get('configured') is True
            and settings.get('effective_settings', {}).get('RFQ', {}).get('effective_approval_required') is True,
            'RFQ_APPROVAL_MUST_BE_ON')
    body = {'expected_revision': candidate.revision, 'candidate_set_id': str(candidate.candidate_set_id),
            'selected_supplier_ids': selected}
    key = 'be3:' + hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
    response = http.put(f"/api/v1/work-cases/{case['id']}/candidate-selection", json=body,
                        headers={'Idempotency-Key': key})
    require(response.status_code == 200, 'SELECTION_FAILED')
    result = CandidateSelectionResponse.model_validate(response.json()['data'])
    require(str(result.case_id) == case['id'] and result.candidate_set_id == candidate.candidate_set_id
            and result.revision == candidate.revision and {str(s) for s in result.selected_supplier_ids} == set(selected),
            'SELECTION_SCOPE_MISMATCH')
    require(all(d.dispatch_state == 'WAITING_APPROVAL' for d in result.drafts), 'DRAFT_NOT_WAITING_APPROVAL')
    for draft in result.drafts:
        saved = data(http, f'/api/v1/mail-drafts/{draft.draft_id}')
        require(saved.get('status') == 'DRAFT' and saved.get('purpose') == 'RFQ'
                and saved.get('case_id') == case['id'] and saved.get('supplier_id') == str(draft.supplier_id)
                and saved.get('provider_message_id') is None and saved.get('sent_at') is None,
                'DRAFT_STATE_OR_SCOPE_MISMATCH')
    candidate_logs = (db.table('workflow_logs').select('id').eq('owner_user_id', str(owner))
        .eq('work_case_id', case['id']).eq('agent_run_id', str(candidate.candidate_set_id)).limit(1).execute().data)
    selection_logs = (db.table('workflow_logs').select('id').eq('owner_user_id', str(owner))
        .eq('work_case_id', case['id']).eq('event_type', 'CANDIDATE_SELECTION')
        .contains('payload_jsonb', {'candidate_set_id': str(candidate.candidate_set_id)}).limit(1).execute().data)
    report.update(selection_performed=True, draft_count=len(result.drafts),
                  all_waiting_approval=True, candidate_log_present=bool(candidate_logs),
                  selection_log_present=bool(selection_logs))
    report['verified'] = bool(candidate_logs and selection_logs)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--case-prefix', required=True)
    parser.add_argument('--supplier-id', action='append', default=[], type=UUID)
    args = parser.parse_args()
    try:
        settings = get_settings()
        token = getpass.getpass('Supabase access token (hidden): ')
        principal = google_principal(token, settings)
        with httpx.Client(base_url='http://127.0.0.1:8000', headers={'Authorization': 'Bearer ' + token},
                          timeout=30, follow_redirects=False, trust_env=False) as http:
            result = verify(http, make_server_client(settings), principal.user_id,
                            args.case_prefix, args.supplier_id)
    except CheckFailed as error:
        result = {'verified': False, 'code': str(error),
                  'note': 'Selection may already be saved; retry uses the same deterministic key. Never approve/send.'}
    except Exception:
        # Do not serialize SDK/HTTP errors or request/response contents.
        result = {'verified': False, 'code': 'BE3_CHECK_FAILED', 'note': 'Check API readiness, approval ON, inventory and logs. Selection may already be saved; retry uses the same deterministic key.'}
    print(json.dumps(result, ensure_ascii=True, indent=2))
    return 0 if result.get('verified') else 1


if __name__ == '__main__':
    raise SystemExit(main())
