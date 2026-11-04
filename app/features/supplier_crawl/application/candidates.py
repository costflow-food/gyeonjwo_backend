"""Collect -> project -> upsert -> read real IDs through the existing GraphStore.

No graph, candidate evaluation, draft or selection flow is reimplemented here.
DB v3 owns standardized keys; no local key producer is required.
"""
from app.agents.tools import TrialOptions
from app.features.supplier_crawl.domain.registration import RegistrationError
from app.features.supplier_crawl.infrastructure.registration import RegistrationFailure


class CrawlSupplierCandidates:
    def __init__(self, *, collect, builder, registrar, store):
        self.collect, self.builder, self.registrar = collect, builder, registrar
        self.store = store

    def _read_registered(self, context, result):
        supplier = self.store.get_supplier(context.case_id, result['supplier_id'])
        if not supplier or supplier['id'] != result['supplier_id']:
            raise RegistrationError('REGISTERED_READBACK_REQUIRED')
        if result['action'] == 'LINKED_REGISTERED':
            if supplier.get('source_type') != 'REGISTERED':
                raise RegistrationError('REGISTERED_READBACK_REQUIRED')
            # Empty IDs are explicitly NOT permission to create/register items.
            return [], ['REGISTERED_ITEM_MAPPING_REQUIRED']
        ids = result['supplier_item_ids']
        mappings = {item['supplier_item_id']: item for item in result['item_mappings']}
        rows = self.store.get_supplier_items(context.case_id, ids)
        if ({row['id'] for row in rows} != set(ids) or len(rows) != len(ids)
                or any(row['supplier_id'] != result['supplier_id'] for row in rows)
                or any('standardized_item_key' in row and
                       row['standardized_item_key'] != mappings[row['id']]['standardized_item_key'] for row in rows)):
            raise RegistrationError('REGISTERED_READBACK_REQUIRED')
        # The current common GraphStore selects no key column. Do not invent
        # a required store API: retain the validated DB mappings in registration.
        # If a store supplies the key, the optional readback above must agree.
        conditions = self.store.get_candidate_conditions(context.case_id)
        if conditions is None or conditions.get('input_revision') != context.input_revision:
            raise RegistrationError('REVISION_CONFLICT')
        # Do not coerce cert objects to strings, PASS or []. Existing team
        # evaluator needs an agreed object-aware UNKNOWN rule first.
        objects = any(any(isinstance(cert, dict) for cert in row.get('certifications_jsonb') or []) for row in rows)
        blockers = ['TEAM_CERTIFICATION_FORMAT_REQUIRED'] if objects else []
        mandatory = conditions.get('mandatory_conditions')
        required_certificates = mandatory.get('certifications') if isinstance(mandatory, dict) else mandatory
        if required_certificates and any(row.get('certifications_jsonb') == [] for row in rows):
            # SQL omits an unknown cert list, but a deployed column default or
            # older row may still read back as []. Preserve it; do not call it
            # a verified negative or overwrite it with None.
            blockers.append('TEAM_WEB_CERTIFICATION_UNKNOWN_REQUIRED')
        if not supplier.get('contact_email'):
            blockers.append('REGISTERED_CONTACT_REQUIRED')
        return ids, blockers

    def run(self, context):
        self.registrar.check(context)  # Scope/revision/role before collection I/O.
        case = self.store.get_candidate_conditions(context.case_id)
        if case is None:
            return {'status': 'CASE_NOT_FOUND'}
        if case.get('input_revision') != context.input_revision:
            return {'status': 'REVISION_CHANGED'}
        keyword = case.get('item_name')
        if not isinstance(keyword, str) or not keyword.strip():
            return {'status': 'CONDITIONS_MISSING'}
        # No arbitrary URLs, secrets, source configuration or owner from the LLM.
        report = self.collect(TrialOptions(keyword=keyword, item_limit=30, business_limit=10,
                                          haccp_pages=3, site_limit=5))
        results, eligible_ids = [], []
        for candidate in report.candidates[:10]:
            result = None
            try:
                payload = self.builder.build(candidate)
                result = self.registrar.register(context, payload)
                if result['action'] == 'CONTACT_REQUIRED':
                    results.append({'registration': result, 'evaluation_item_ids': [], 'blockers': []})
                    continue
                ids, blockers = self._read_registered(context, result)
                # Registration truth keeps its real IDs; evaluation handoff
                # excludes blocked results in both the per-result and total list.
                evaluation_ids = [] if blockers else list(ids)
                results.append({'registration': result, 'evaluation_item_ids': evaluation_ids, 'blockers': blockers})
                eligible_ids.extend(evaluation_ids)
            except RegistrationError as error:
                results.append({'status': 'REGISTRATION_BLOCKED' if result is None else 'REGISTERED_READBACK_BLOCKED',
                                'code': error.code, 'registration': result, 'evaluation_item_ids': [],
                                'outcome_unknown': isinstance(error, RegistrationFailure) and error.outcome_unknown})
                if error.code in {'REVISION_CONFLICT', 'RESOURCE_NOT_FOUND', 'CRAWL_SCOPE_NOT_ALLOWED'}:
                    break
                if isinstance(error, RegistrationFailure) and error.outcome_unknown:
                    break
        return {'status': 'COLLECTED', 'results': results,
                # These IDs still require the normal search_supplier_evidence
                # and create_rfq_draft tool calls. They are not CandidateOut.
                'evaluation_item_ids': list(dict.fromkeys(eligible_ids)),
                'source_issues': [{'source_type': issue.source_type, 'code': issue.code} for issue in report.issues],
                'candidate_limit_reached': len(report.candidates) > 10}
