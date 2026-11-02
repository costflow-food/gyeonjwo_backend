"""Read-only RFQ readiness: python -m scripts.check_rfq_runtime [--db]."""
import argparse
import json
import httpx

from app.features.work_cases.application.rfq_preparation import CANDIDATE_PROMPT
from app.core.config import get_settings

RPC_ARGUMENTS = {
    'rfq_schema_health': set(),
    'rfq_http_health': set(),
    'submit_rfq_draft_action': {'p_owner', 'p_draft', 'p_action', 'p_revision', 'p_snapshot',
                              'p_settings_version', 'p_key', 'p_hash', 'p_request_id', 'p_accepting'},
    'list_rfq_reconcile_requests': {'p_owner', 'p_limit'},
    'finish_rfq_reconcile_request': {'p_owner', 'p_event', 'p_code'},
    'recover_rfq_server': {'p_owner'},
    'case_extraction_health': set(),
    'submit_work_case_start': {'p_owner', 'p_case', 'p_revision', 'p_key', 'p_hash', 'p_request_id'},
    'claim_case_extraction': set(),
    'heartbeat_case_extraction': {'p_job', 'p_token'},
    'complete_case_extraction': {'p_job', 'p_token', 'p_revision', 'p_batch', 'p_output', 'p_result', 'p_model', 'p_prompt_version'},
    'fail_case_extraction': {'p_job', 'p_token', 'p_code'},
    'recover_case_extractions': set(),
    'get_work_case_reply_facts': {'p_owner', 'p_case_ids'},
    'open_rfq_round': {'p_owner', 'p_case', 'p_expected_revision', 'p_expected_current_round', 'p_suppliers', 'p_key'},
    'claim_rfq_outbound_metadata': {'p_owner', 'p_limit'},
    'complete_rfq_outbound_metadata': {'p_owner', 'p_sent', 'p_token', 'p_message'},
    'fail_rfq_outbound_metadata': {'p_owner', 'p_sent', 'p_token'},
    'retry_rfq_outbound_metadata': {'p_owner', 'p_sent'},
    'claim_mail_send': {'p_owner', 'p_draft'},
    'finish_mail_send': {'p_owner', 'p_draft', 'p_result', 'p_provider_id', 'p_error'},
    'reconcile_mail_send': {'p_owner', 'p_draft', 'p_result', 'p_evidence', 'p_provider_id'},
    'set_review_step': {'p_owner', 'p_case', 'p_revision', 'p_batch', 'p_step', 'p_error'},
}


def report(settings, *, check_db=False, transport=None):
    missing = []
    if not CANDIDATE_PROMPT.strip():
        missing.append('CANDIDATE_PROMPT')
    result = {'status': 'not_ready' if missing else 'implementation_present',
              'implementation_mode': 'server_candidate_pipeline',
              'missing_implementation': missing, 'database': 'not_checked',
              'integration_verified': False, 'remote_mutations': False}
    if not check_db:
        return result
    if not settings.supabase_url or not settings.supabase_secret_key:
        result['database'] = 'configuration_required'
        result['status'] = 'not_ready'
        return result
    try:
        key = settings.supabase_secret_key.get_secret_value()
        with httpx.Client(transport=transport, timeout=20, follow_redirects=False) as client:
            response = client.get(str(settings.supabase_url).rstrip('/') + '/rest/v1/',
                headers={'apikey': key, 'Authorization': 'Bearer ' + key, 'Accept': 'application/openapi+json'})
        response.raise_for_status()
        schema = response.json()
        missing_rpc, mismatched = [], []
        for name, arguments in RPC_ARGUMENTS.items():
            path = schema['paths'].get('/rpc/' + name)
            if path is None:
                missing_rpc.append(name)
                continue
            body = next((p.get('schema', {}) for p in path.get('post', {}).get('parameters', [])
                         if p.get('in') == 'body'), {})
            if '$ref' in body:
                body = schema['definitions'][body['$ref'].rsplit('/', 1)[-1]]
            if not arguments <= body.get('properties', {}).keys():
                mismatched.append(name)
        result['database'] = 'schema_not_ready' if missing_rpc or mismatched else 'schema_present'
        result['missing_rpc'], result['mismatched_rpc'] = missing_rpc, mismatched
        # Even present tools/schema are not proof of producer persistence or send.
        result['status'] = 'not_ready' if missing or missing_rpc or mismatched else 'requires_integration_verification'
    except Exception:
        result['database'] = 'discovery_unavailable'
        result['status'] = 'not_ready'
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', action='store_true')
    args = parser.parse_args()
    print(json.dumps(report(get_settings(), check_db=args.db), ensure_ascii=False, indent=2))
    return 1  # This diagnostic never certifies end-to-end sending.


if __name__ == '__main__':
    raise SystemExit(main())
