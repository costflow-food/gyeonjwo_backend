"""Read-only contract discovery. Never treats schema presence as an E2E pass."""
import json
import httpx

from app.core.config import ROOT


def check_extraction_schema(settings, *, transport=None, contract=None):
    """GET the PostgREST schema only; do not execute jobs or business RPCs.

    Output contains fixed configuration/column/function/argument names only.
    It excludes credentials, provider responses, user rows and source content.
    """
    result = {'status': 'schema_not_ready', 'missing': [], 'mismatched': [],
              'integration_verified': False, 'remote_mutations': False}
    if not settings.supabase_url:
        result['missing'].append('SUPABASE_URL')
    if not settings.supabase_secret_key:
        result['missing'].append('SUPABASE_SECRET_KEY')
    if result['missing']:
        return result
    if contract is None:
        contract = json.loads((ROOT / 'integration_contracts' / 'extraction_runtime.json').read_text(encoding='utf-8'))
    try:
        key = settings.supabase_secret_key.get_secret_value()
        with httpx.Client(transport=transport, timeout=20, follow_redirects=False) as client:
            response = client.get(str(settings.supabase_url).rstrip('/') + '/rest/v1/',
                                  headers={'apikey': key, 'Authorization': 'Bearer ' + key,
                                           'Accept': 'application/openapi+json'})
        if response.status_code != 200:
            result['error_code'] = 'SCHEMA_DISCOVERY_UNAVAILABLE'
            return result
        schema = response.json()
        if not isinstance(schema, dict) or not isinstance(schema.get('paths'), dict):
            raise ValueError('Invalid schema')
    except Exception:
        result['error_code'] = 'SCHEMA_DISCOVERY_UNAVAILABLE'
        return result
    fields = schema.get('definitions', {}).get('work_cases', {}).get('properties', {})
    if 'requester_name' not in fields:
        result['missing'].append('work_cases.requester_name')
    for name, expected in contract['rpc'].items():
        path = schema['paths'].get('/rpc/' + name)
        if path is None:
            result['missing'].append('rpc.' + name)
            continue
        parameters = path.get('post', {}).get('parameters', [])
        actual = next((item.get('schema', {}).get('properties', {}) for item in parameters
                       if item.get('name') == 'args' and item.get('in') == 'body'), {})
        for argument in expected.get('args', {}):
            if argument not in actual:
                result['mismatched'].append('rpc.' + name + '.' + argument)
        # Unexpected required arguments also make the Python caller incompatible.
        required = next((item.get('schema', {}).get('required', []) for item in parameters
                         if item.get('name') == 'args' and item.get('in') == 'body'), [])
        if set(required) - set(expected.get('args', {})):
            result['mismatched'].append('rpc.' + name + '.unexpected_required_arguments')
    if not result['missing'] and not result['mismatched']:
        result['status'] = 'schema_ready'
    return result
