"""Narrow service-only client for the already applied, five-argument RPC.

No SQL, table writes, supplier creation fallback or automatic mutation retry.
"""
from copy import deepcopy
from uuid import UUID

from app.features.supplier_crawl.domain.registration import RegistrationError, RegistrationPayload


SQL_ERRORS = frozenset('''INVALID_WEB_INPUT WEB_INPUT_LIMIT UNKNOWN_WEB_FIELD RESOURCE_NOT_FOUND
REVISION_CONFLICT INVALID_WEB_SUPPLIER INVALID_CONTACT_EMAIL MISSING_WEB_EVIDENCE
INVALID_WEB_EVIDENCE INVALID_WEB_PROVENANCE MISSING_CONTACT_EVIDENCE ORPHAN_WEB_EVIDENCE
INVALID_HOMEPAGE DUPLICATE_WEB_REPORT INVALID_WEB_ITEM INVALID_HACCP_EVIDENCE
INVALID_HACCP_PERIOD STALE_WEB_OBSERVATION STALE_WEB_FIELD STALE_WEB_ITEM
ITEM_KEY_MISMATCH AMBIGUOUS_ITEM_KEY'''.split())


class RegistrationFailure(RegistrationError):
    def __init__(self, code, *, outcome_unknown=False):
        self.outcome_unknown = outcome_unknown
        super().__init__(code)


def _uuid(value):
    if not isinstance(value, str):
        raise ValueError('Invalid UUID')
    return str(UUID(value))


def validate_result(result, payload):
    """Match the SQL's three distinct response shapes without adding defaults."""
    try:
        if not isinstance(result, dict):
            raise ValueError()
        action = result['action']
        if action == 'CONTACT_REQUIRED':
            if (set(result) != {'action', 'candidate_id', 'supplier_id', 'supplier_item_ids', 'item_mappings', 'rfq_eligible'}
                    or 'contact_email' in payload.supplier or result['supplier_id'] is not None
                    or result['rfq_eligible'] is not False or result['supplier_item_ids'] != []
                    or result['item_mappings'] != []):
                raise ValueError()
            _uuid(result['candidate_id'])
        elif action in {'CREATED', 'UPDATED', 'LINKED_REGISTERED'}:
            if (set(result) != {'action', 'supplier_id', 'supplier_item_ids', 'item_mappings', 'requires_item_mapping'}
                    or 'contact_email' not in payload.supplier):
                raise ValueError()
            _uuid(result['supplier_id'])
            if action == 'LINKED_REGISTERED':
                if (result['supplier_item_ids'] != [] or result['item_mappings'] != []
                        or result['requires_item_mapping'] is not True):
                    raise ValueError()
            else:
                expected = [item['report_no'] for item in payload.items]
                mappings, ids = result['item_mappings'], result['supplier_item_ids']
                if (result['requires_item_mapping'] is not False or not isinstance(mappings, list)
                        or not isinstance(ids, list) or not expected or len(expected) != len(set(expected))
                        or len(mappings) != len(expected) or len(ids) != len(expected)):
                    raise ValueError()
                canonical_ids = [_uuid(value) for value in ids]
                if len(canonical_ids) != len(set(canonical_ids)):
                    raise ValueError()
                by_report = {}
                mapped_ids = []
                for row in mappings:
                    if (not isinstance(row, dict)
                            or set(row) != {'report_no', 'supplier_item_id', 'standardized_item_key'}
                            or not isinstance(row['report_no'], str) or row['report_no'] not in expected
                            or row['report_no'] in by_report
                            or not isinstance(row['standardized_item_key'], str)
                            or not row['standardized_item_key'].strip()):
                        raise ValueError()
                    by_report[row['report_no']] = row
                    mapped_ids.append(_uuid(row['supplier_item_id']))
                if (set(by_report) != set(expected) or len(set(mapped_ids)) != len(mapped_ids)
                        or set(mapped_ids) != set(canonical_ids)):
                    raise ValueError()
                for item in payload.items:
                    # Compatibility for explicit callers: a success must echo
                    # the exact provided key. Normal collection omits it.
                    if ('standardized_item_key' in item and
                            (not isinstance(item['standardized_item_key'], str) or
                             item['standardized_item_key'] != by_report[item['report_no']]['standardized_item_key'])):
                        raise ValueError()

        else:
            raise ValueError()
    except (KeyError, ValueError, TypeError, AttributeError):
        # The server could already have committed. Do not silently retry a write.
        raise RegistrationFailure('INVALID_WEB_RPC_RESULT', outcome_unknown=True) from None
    return deepcopy(result)


class WebSupplierRegistrar:
    def __init__(self, scope):
        self.scope = scope

    def check(self, context):
        scope = self.scope
        scope.check(context.case_id, context.input_revision)
        store = scope.agent_store
        if (not context.agent_run_id or not store
                or store.role_runs.get(context.agent_run_id) != 'CANDIDATE'):
            raise RegistrationError('CRAWL_SCOPE_NOT_ALLOWED')

    def register(self, context, payload: RegistrationPayload):
        self.check(context)
        params = {'p_owner': _uuid(self.scope.owner), 'p_case': _uuid(self.scope.case_id),
                  'p_expected_revision': context.input_revision,
                  'p_supplier': deepcopy(payload.supplier), 'p_items': deepcopy(payload.items)}
        try:
            result = self.scope.repo.client.rpc('upsert_web_supplier', params).execute().data
        except Exception as error:
            code, message = getattr(error, 'code', None), getattr(error, 'message', None)
            if code == '40001':
                safe = 'REVISION_CONFLICT'
            elif code == 'P0002':
                safe = 'RESOURCE_NOT_FOUND'
            elif code in {'22023', '22007', '22008', '42501', '55000'}:
                safe = message if message in SQL_ERRORS else 'WEB_REGISTRATION_REJECTED'
            else:
                raise RegistrationFailure('WEB_REGISTRATION_UNAVAILABLE', outcome_unknown=True) from None
            raise RegistrationFailure(safe) from None
        return validate_result(result, payload)
