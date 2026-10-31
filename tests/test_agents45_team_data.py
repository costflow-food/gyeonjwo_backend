"""Team-shaped synthetic records; real mapping, registry, runner and calculations.

Only model/storage doubles. Supplement and validation stores below are explicit
unagreed dependency doubles, not implementations of missing common tools.
"""
from copy import deepcopy
from dataclasses import replace
import json
from threading import RLock
from uuid import UUID

import pytest

from agent_memory_store import MemoryStore, RoleMemoryStore
from test_agents45_product_registration import ToolDrivenModel
from test_agents45_product_registration import forbid_final_writes
from app.agents import tools
from app.agents.agents import AgentRunner, PROMPTS
from app.agents.schemas import AgentRole as R, PlanIn, CounterOfferIn
from app.agents.tools import calculation_digest as digest
from app.agents.tools import OutputValidationError as BridgeError, ExecutionScope as Scope
from app.agents.tools import _ReviewAccess as ReviewAccess, review_record_digest as team_record_digest
from app.agents.tools import MissingReviewData as MissingTeamData
from app.agents.agents import validate_review_output
from app.agents.tools import ReviewCalculator as BridgeService, model_payload as wire
from app.agents.schemas import RunContext

CASE = '15582535-f1b8-5326-8dc9-978888285fd0'


class SupplementMemory:
    def __init__(self, common, scope, facts):
        self.common, self.scope, self.facts = common, scope, facts
        self.refresh_binding()

    def refresh_binding(self):
        self.record_digest = team_record_digest(self.common.get_work_case(CASE), self.common.quote_versions)

    def load(self, scope):
        return {'case_id': self.scope.case_id, 'input_revision': self.scope.revision,
                'quote_version_ids': list(self.scope.quote_ids), 'team_record_digest': self.record_digest,
                'facts': deepcopy(self.facts)}


class ValidationMemory:
    def __init__(self):
        self.records = {}
        self.lock = RLock()
        self.repository = None
        self.before_write = None

    def _current(self, scope, source_digest):
        if digest(self.repository.for_run(RunContext(scope.case_id, scope.revision)).snapshot(scope)) != source_digest:
            raise BridgeError('REFERENCE_SOURCE_CHANGED')

    def put_if_current(self, scope, source_digest, kind, payload, agent_run_id):
        with self.lock:
            if self.before_write:
                self.before_write()
            self._current(scope, source_digest)
            row = {'scope': vars(scope), 'source_digest': source_digest, 'kind': kind,
                   'payload': deepcopy(payload), 'agent_run_id': agent_run_id}
            ref = 'SYNTHETIC-validation:' + digest(row)
            self.records[ref] = row
            return ref

    def resolve(self, scope, source_digest, kind, ref):
        with self.lock:
            self._current(scope, source_digest)
            row = self.records.get(ref)
            if row is None or row['scope'] != vars(scope) or row['kind'] != kind:
                raise BridgeError('REFERENCE_SCOPE_MISMATCH')
            if row['source_digest'] != source_digest or ref != 'SYNTHETIC-validation:' + digest(row):
                raise BridgeError('REFERENCE_SOURCE_CHANGED')
            return deepcopy(row['payload'])


def configure_review_stores(common, *, supplements=None, references=None):
    roles = RoleMemoryStore(common, supplements, references)
    tools.use_role_stores(priority=roles, plans=roles, counter_offers=roles)


@pytest.fixture
def team_storage():
    common = MemoryStore()
    policy = {'policy_id': 'SYNTHETIC-policy', 'source_ref': 'SYNTHETIC-policy-source', 'approved': True,
              'valid_from': '2026-11-02', 'valid_to': '2026-11-30', 'allow_split': True,
              'max_suppliers': 2, 'allocation_step': '100', 'min_unit_price': '5000', 'max_unit_price': '5500',
              'max_total_cost': '5500000', 'price_increment': '1',
              'skip_if_request_not_more_than_alternative': True, 'allow_partial_comparison': False}
    common.add_case(CASE, item_name='냉동딸기', specification='A등급 IQF', quantity='1000', unit='kg',
                    due_date='2026-11-20', mandatory_conditions={}, policy=policy)
    period = {'from': '2026-11-20', 'to': '2026-11-20'}
    terms = {'delivery_location': 'SYNTHETIC 센터', 'delivery_terms': '1회 납품',
             'payment_terms': '검수 후 30일', 'tax_basis': 'EXCLUDED'}
    values = {'as_of': '2026-11-02', 'period': period,
              'item_identity': {'item_id': 'strawberry', 'origin': 'KR', 'grade': 'A', 'processing': 'FROZEN'},
              'terms': terms, 'incumbent_supplier_id': 'supplier-a',
              'fixed_commitment': {'supplier_id': 'supplier-a', 'quantity': '0', 'unit_price': '5000',
                                   'period': period, 'verified': True},
              'repricing_scope': 'ALL_INCUMBENT_CHANGEABLE', 'policy_source': policy}
    for qid, supplier, price, qty, shipping in [('qv-a', 'supplier-a', '5500', '1000', '0'),
                                               ('qv-b', 'supplier-b', '5400', '600', '10000')]:
        mid = 'mail-' + qid
        fields = {'unit_price': price, 'unit': 'kg', 'available_quantity': qty, 'moq': '0',
                  'effective_from': '2026-11-01', 'valid_until': '2026-11-30', 'currency': 'KRW',
                  'item_name': '냉동딸기', 'specification': 'A등급 IQF', 'origin': 'KR', 'tax_terms': '부가세 별도',
                  'delivery_date': '2026-11-20', 'lead_time': '0일'}
        if qid == 'qv-a':
            fields['previous_unit_price'] = '5000'
        evidence = {k: {'raw_value': v, 'value': v, 'mail_id': mid, 'location': mid + ' 본문'}
                    for k, v in fields.items()}
        costs = [{'cost_type': kind, 'source_text': f'{kind}: {amount}원 비교기간 전체 구매사 별도 부담',
                  'amount': amount, 'basis': '비교기간 전체', 'payer': '구매사', 'included_in_price': False,
                  'mail_id': mid, 'location': mid + ' 본문'}
                 for kind, amount in [('SHIPPING', shipping), ('HANDLING', '0')]]
        common.add_mail(mid, CASE, 'SYNTHETIC\n' + '\n'.join(k + ': ' + v for k, v in fields.items()) +
                        '\n' + '\n'.join(c['source_text'] for c in costs), supplier_id=supplier)
        common.quote_versions[qid] = tools.QuoteVersionRecord(
            work_case_id=CASE, supplier_id=supplier, source_mail_id=mid, version_no=1, supersedes_id=None,
            unit_price=price, unit='kg', shipping_cost=shipping, available_quantity=qty,
            effective_from='2026-11-01', valid_until='2026-11-30',
            checks_jsonb={k: {'state': 'STATED', 'value': fields[k]} for k in
                          ('item_name', 'specification', 'origin', 'tax_terms', 'delivery_date', 'lead_time')},
            pricing_terms_jsonb={'currency': 'KRW', 'moq': '0'}, cost_terms_jsonb=costs, item_notes_jsonb=[],
            evidence_jsonb={k: v for k, v in evidence.items() if k != 'previous_unit_price'},
            extraction_status='CONFIRMED', mail_normalized={mid: {'schema_version': tools.SCHEMA_VERSION,
                'fields': evidence, 'item_notes': []}}, case_conditions=None)
        values.update({qid + '.item_identity': {'item_id': 'strawberry', 'grade': 'A', 'processing': 'FROZEN'},
                       qid + '.terms': {k: v for k, v in terms.items() if k != 'tax_basis'},
                       qid + '.valid_from': '2026-10-29',
                       qid + '.switching_cost': '0', qid + '.cost_scope': 'PER_SUPPLIER_PER_PERIOD',
                       qid + '.eligibility': 'CONFIRMED'})
    # Missing team fields are explicit, source-bound dependency data. This is not
    # a full engine snapshot: no quotes/plans/bindings/calculations are injected.
    facts = {k: {'value': v, 'evidence': {'mail_id': 'review-facts', 'part': 'body', 'page': None,
             'excerpt': k + '=' + json.dumps(v, ensure_ascii=False, sort_keys=True)}} for k, v in values.items()}
    common.add_mail('review-facts', CASE, 'SYNTHETIC review facts, not a team-approved contract\n' +
                    '\n'.join(e['evidence']['excerpt'] for e in facts.values()))
    scope = Scope(CASE, 1, ('qv-a', 'qv-b'))
    supplement = SupplementMemory(common, scope, facts)
    refs = ValidationMemory()
    repo = ReviewAccess(RunContext(CASE, scope.revision), R.RESPONSE_PLAN)
    refs.repository = repo
    tools.use_case_store(common)
    tools.use_quote_store(common)
    configure_review_stores(common, supplements=supplement, references=refs)
    yield common, scope, supplement, refs, repo
    tools.use_role_stores()
    tools.use_case_store(None)
    tools.use_quote_store(None)


def run_plan(storage, model=None, scope=None):
    common, original, _, _, _ = storage
    scope = scope or original
    return AgentRunner(model or ToolDrivenModel(messages=iter([])), runs=common)(R.RESPONSE_PLAN,
        PlanIn(case_id=UUID(scope.case_id), input_revision=scope.revision, quote_version_ids=list(scope.quote_ids)))


def test_team_records_through_product_registration_to_stage4_and_stage5(team_storage):
    common, scope, _, refs, repo = team_storage
    before = dict(tools._registry)
    mapped = repo.snapshot(scope)
    assert mapped['data']['request_id'] == CASE and mapped['data']['changeable_quantity'] == '1000'
    assert mapped['data']['quotes'][1]['costs']['transport'] == '10000'
    assert mapped['notice_quote_ids'] == ['qv-a'] and set(mapped['quote_bindings']) == set(scope.quote_ids)
    assert mapped['notice_basis']['previous_unit_price'] == '5000'
    assert all(e.get('text', 'SYNTHETIC').startswith('SYNTHETIC') for e in mapped['evidence'].values())
    plan = run_plan(team_storage)
    assert plan.next_action == 'PLANS_PROPOSED' and plan.plans
    assert {a.quote_version_id for p in plan.plans for a in p.allocations} == {'qv-a', 'qv-b'}
    out = AgentRunner(ToolDrivenModel(messages=iter([])), runs=common)(R.COUNTER_OFFER,
        CounterOfferIn(case_id=UUID(CASE), input_revision=1, quote_version_ids=list(scope.quote_ids),
                       validated_plans=plan.plans))
    price = [r for r in refs.records.values() if r['kind'] == 'PRICE_VALIDATION'][-1]
    assert price['payload']['status'] == 'PASS'
    assert price['payload']['target']['target_unit_price'] == '5416'
    assert price['payload']['target']['basis_quote_version_ids'] == ['qv-a']
    assert price['agent_run_id'] == 'run-2'
    assert out.next_action == 'HOLD' and out.unknown_fields == ['create_counter_offer_draft']
    assert out.target is None and out.draft_ids == []
    assert common.agent_runs['run-1']['output_jsonb'] == plan.model_dump(mode='json')
    assert common.agent_runs['run-2']['output_jsonb'] == out.model_dump(mode='json')
    assert not any(r['kind'] in ('AGENT_RESULT', 'DRAFT') for r in refs.records.values())
    assert before == tools._registry and 'create_counter_offer_draft' not in before and 'check_comparability' in before


@pytest.mark.parametrize('fault,expected', [
    ('policy', 'policy'), ('cost', 'cost_terms'),
    ('facts', 'review_facts.fixed_commitment'), ('references', 'PlanStore.save_plan_set'),
    ('all_facts', 'cost_terms.comparison_period_required'),
    ('suppliers', 'QuoteStore.get_quote_version(case_id, quote_version_id)'),
])
def test_missing_dependencies_hold_without_defaults(team_storage, fault, expected):
    common, _, supplement, refs, _ = team_storage
    if fault == 'policy':common.work_cases[CASE]['policy'] = None
    if fault == 'cost':common.quote_versions['qv-b'].cost_terms_jsonb = []
    if fault == 'facts':supplement.facts.pop('fixed_commitment')
    if fault == 'references':configure_review_stores(common, supplements=supplement)
    if fault == 'all_facts':configure_review_stores(common, references=refs)
    if fault == 'suppliers':common.mail_messages['mail-qv-b']['supplier_id'] = None
    supplement.refresh_binding()
    out = run_plan(team_storage)
    # The removed structured costs still have answers in the source mail.
    assert out.next_action == ('RECHECK_SOURCE' if fault == 'cost' else 'HOLD') and expected in out.unknown_fields
    assert not refs.records


@pytest.mark.parametrize('fault,code', [
    ('case', 'CASE_SCOPE_QUOTE_MISMATCH'), ('quote', 'STALE_QUOTE_VERSIONS'),
    ('supplier_source', 'CASE_SCOPE_EVIDENCE_MISMATCH'),
    ('supplement', 'REFERENCE_SCOPE_SUPPLEMENT_MISMATCH'), ('record_change', 'REFERENCE_SOURCE_SUPPLEMENT_CHANGED'),
])
def test_wrong_case_quote_or_original_evidence_is_blocked(team_storage, fault, code):
    common, scope, supplement, refs, _ = team_storage
    if fault == 'case':
        common.latest_quote_version = lambda c, s: ('qv-a', common.quote_versions['qv-a'].model_copy(update={'work_case_id': 'foreign'}))
    if fault == 'quote':scope = Scope(CASE, 1, ('foreign',))
    if fault == 'supplier_source':
        common.quote_versions['qv-a'].evidence_jsonb['unit_price']['mail_id'] = 'mail-qv-b'
        supplement.refresh_binding()
    if fault == 'supplement':supplement.scope = Scope('foreign', 1, scope.quote_ids)
    if fault == 'record_change':common.work_cases[CASE]['mandatory_conditions'] = {'certification': 'new'}
    with pytest.raises(Exception, match=code):run_plan(team_storage, scope=scope)
    assert not refs.records


@pytest.mark.parametrize('when', ['read', 'write'])
def test_revision_change_is_checked_on_reads_and_atomic_reference_writes(team_storage, when):
    common, _, _, refs, _ = team_storage
    def advance():common.work_cases[CASE]['input_revision'] += 1
    model = ToolDrivenModel(messages=iter([]), hook=advance if when == 'read' else None)
    if when == 'write':refs.before_write = advance
    with pytest.raises(Exception, match='STALE_REVISION'):run_plan(team_storage, model)
    assert not refs.records


def test_price_mutation_is_not_overridden_by_completed_fixture_input(team_storage):
    common, scope, supplement, _, repo = team_storage
    row = common.quote_versions['qv-b']
    row.unit_price = '5350'
    row.evidence_jsonb['unit_price'].update(value='5350', raw_value='5350')
    common.mail_messages['mail-qv-b']['body_text'] = common.mail_messages['mail-qv-b']['body_text'].replace('5400', '5350')
    supplement.refresh_binding()
    assert repo.snapshot(scope)['data']['quotes'][1]['unit_price'] == '5350'
    plan = run_plan(team_storage)
    assert min(p.total_amount for p in plan.plans if p.plan_type == 'SPLIT') == '5420000'


def test_validation_record_tamper_rejected_in_counter_stage(team_storage):
    common, scope, _, refs, _ = team_storage
    plan = run_plan(team_storage)
    refs.records[plan.plans[0].calculation_ref]['payload']['status'] = 'FORGED'
    with pytest.raises(Exception, match='REFERENCE_SOURCE_CHANGED'):
        AgentRunner(ToolDrivenModel(messages=iter([])), runs=common)(R.COUNTER_OFFER,
            CounterOfferIn(case_id=UUID(CASE), input_revision=1, quote_version_ids=list(scope.quote_ids),
                           validated_plans=plan.plans))


def test_counter_prompt_is_conditional_on_real_dependency_result():
    prompt = PROMPTS[R.COUNTER_OFFER]
    assert 'missing_dependencies' in prompt and 'COUNTER_OFFER_PROPOSED' in prompt
    assert '성공해도 이번 연결에서는 HOLD' not in prompt


@pytest.mark.parametrize('fault', ['price', 'cost', 'included', 'missing_raw'])
def test_normalized_values_must_still_match_original_source(team_storage, fault):
    common, _, supplement, refs, _ = team_storage
    row = common.quote_versions['qv-b']
    if fault == 'price':
        row.unit_price = '5300'
        row.evidence_jsonb['unit_price']['value'] = '5300'
    if fault == 'cost':row.cost_terms_jsonb[0]['amount'] = '9000'
    if fault == 'included':row.cost_terms_jsonb[0]['included_in_price'] = True
    if fault == 'missing_raw':row.evidence_jsonb['item_name']['raw_value'] = None
    supplement.refresh_binding()
    out = run_plan(team_storage)
    assert out.next_action == 'RECHECK_SOURCE'
    assert out.unknown_fields == [{'price': 'unit_price', 'missing_raw': 'item_name'}.get(fault, 'cost_terms')]
    assert set(out.unknown_fields) <= tools.EXTRACTION_FIELD_NAMES
    assert not refs.records


@pytest.mark.parametrize('price,expected', [('1.01', '1.01'), ('1.005', '1.01')])
def test_shared_calculation_rounding_difference_is_not_silently_accepted(price, expected):
    from app.agents.tools import verify_common_total as verify_team_total
    row = {'lines': [{'unit_price': price, 'quantity': '1'}], 'supplier_costs': [], 'total_cost': expected}
    assert str(verify_team_total(row)) == expected
    row['total_cost'] = '0'
    with pytest.raises(BridgeError, match='TEAM_CALCULATION_MISMATCH'):
        verify_team_total(row)


def test_irrelevant_supplements_are_not_needed_for_same_plans_and_price(team_storage):
    common, scope, supplement, refs, repo = team_storage
    for key in ('policy_source', 'item_identity', 'incumbent_supplier_id', 'qv-a.item_identity',
                'qv-b.item_identity', 'qv-a.switching_cost', 'qv-a.cost_scope', 'qv-b.cost_scope',
                'qv-a.valid_from', 'qv-b.valid_from'):
        supplement.facts.pop(key)
    supplement.facts['as_of'].pop('evidence')
    supplement.facts['qv-b.switching_cost']['value'] = '0.000'
    fixed = supplement.facts['fixed_commitment']['value']
    fixed.pop('unit_price');fixed.pop('period')
    for qid, row in common.quote_versions.items():
        row.checks_jsonb.pop('lead_time');row.evidence_jsonb.pop('lead_time')
        row.mail_normalized[row.source_mail_id]['fields'].pop('lead_time')
        common.mail_messages[row.source_mail_id]['body_text'] = common.mail_messages[row.source_mail_id]['body_text'].replace('lead_time: 0일', '')
    row = common.quote_versions['qv-b']
    row.effective_from = None;row.evidence_jsonb.pop('effective_from')
    supplement.refresh_binding()
    mapped = repo.snapshot(scope)['data']
    assert 'unit_price' not in mapped['fixed_commitment']
    assert 'item_id' not in mapped['item']
    assert mapped['quotes'][0]['costs']['switching'] is None
    assert all(q['lead_days'] is None and q['valid_from'] is None for q in mapped['quotes'])
    out = run_plan(team_storage)
    assert out.next_action == 'PLANS_PROPOSED'
    result = AgentRunner(ToolDrivenModel(messages=iter([])), runs=common)(R.COUNTER_OFFER,
        CounterOfferIn(case_id=UUID(CASE), input_revision=1, quote_version_ids=list(scope.quote_ids), validated_plans=out.plans))
    assert result.next_action == 'HOLD' and result.unknown_fields == ['create_counter_offer_draft']
    assert [r for r in refs.records.values() if r['kind'] == 'PRICE_VALIDATION'][-1]['payload']['target']['target_unit_price'] == '5416'


@pytest.mark.parametrize('alternative_capacity,kinds', [('600', {'KEEP'}), ('1000', {'KEEP', 'SWITCH'})])
def test_no_split_requires_no_split_repricing_rule_and_keep_can_stand_alone(team_storage, alternative_capacity, kinds):
    common, scope, supplement, _, repo = team_storage
    common.work_cases[CASE]['policy'].update(allow_split=False, max_suppliers=1)
    supplement.facts.pop('repricing_scope')
    row = common.quote_versions['qv-b']
    row.available_quantity = alternative_capacity
    row.evidence_jsonb['available_quantity'].update(value=alternative_capacity, raw_value=alternative_capacity)
    common.mail_messages[row.source_mail_id]['body_text'] = common.mail_messages[row.source_mail_id]['body_text'].replace('available_quantity: 600', 'available_quantity: ' + alternative_capacity)
    supplement.refresh_binding()
    assert repo.snapshot(scope)['data']['repricing_scope'] is None
    result = run_plan(team_storage)
    assert result.next_action == 'PLANS_PROPOSED'
    assert {p.plan_type for p in result.plans} == kinds


@pytest.mark.parametrize('fault,action,field', [
    ('missing_price', 'NEEDS_EVIDENCE', 'unit_price'),
    ('unextracted_price', 'RECHECK_SOURCE', 'unit_price'),
    ('changed_source', 'RECHECK_SOURCE', 'unit_price'),
    ('missing_price_and_policy', 'HOLD', 'policy'),
    ('unsupported_cost_basis', 'NEEDS_EVIDENCE', 'cost_terms'),
])
def test_missing_facts_extraction_and_configuration_use_distinct_actions(team_storage, fault, action, field):
    common, _, supplement, refs, _ = team_storage
    row = common.quote_versions['qv-b']
    if fault in ('missing_price', 'unextracted_price', 'missing_price_and_policy'):
        row.unit_price = None
        if fault != 'unextracted_price':row.evidence_jsonb.pop('unit_price')
        if fault == 'missing_price':
            common.mail_messages[row.source_mail_id]['body_text'] = common.mail_messages[row.source_mail_id]['body_text'].replace('unit_price: 5400', 'unit_price: 미확인')
    if fault == 'missing_price_and_policy':common.work_cases[CASE]['policy'] = None
    if fault == 'changed_source':
        common.mail_messages[row.source_mail_id]['body_text'] = common.mail_messages[row.source_mail_id]['body_text'].replace('unit_price: 5400', 'unit_price: 미확인')
    if fault == 'unsupported_cost_basis':
        row.cost_terms_jsonb[0]['basis'] = '박스당'
        row.cost_terms_jsonb[0]['source_text'] = row.cost_terms_jsonb[0]['source_text'].replace('비교기간 전체', '박스당')
        common.mail_messages[row.source_mail_id]['body_text'] = common.mail_messages[row.source_mail_id]['body_text'].replace('SHIPPING: 10000원 비교기간 전체', 'SHIPPING: 10000원 박스당')
    if fault == 'split_scope':supplement.facts.pop('repricing_scope')
    supplement.refresh_binding()
    result = run_plan(team_storage)
    assert result.next_action == action and field in result.unknown_fields
    if action == 'RECHECK_SOURCE':assert set(result.unknown_fields) <= tools.EXTRACTION_FIELD_NAMES
    if action == 'HOLD':assert 'unit_price' not in result.unknown_fields
    assert not refs.records


@pytest.mark.parametrize('field', ['policy', 'create_counter_offer_draft', 'PlanStore.read_plan_calculation'])
def test_upstream_development_questions_are_not_sent_to_supplier(team_storage, field):
    common, scope, _, refs, _ = team_storage
    out = AgentRunner(ToolDrivenModel(messages=iter([])), runs=common)(R.RESPONSE_PLAN,
        PlanIn(case_id=UUID(CASE), input_revision=1, quote_version_ids=list(scope.quote_ids),
               questions=[{'supplier_id': 'supplier-a', 'field': field, 'priority': 'MUST_CONFIRM', 'reason': 'synthetic'}]))
    assert out.next_action == 'HOLD' and out.unknown_fields == [field]
    assert not refs.records


def _shared_total(prices, transport='0', other='0'):
    from decimal import Decimal
    from app.features.review.domain.calculations import evaluate
    from app.features.review.domain.schemas import CalculationProposal
    registry = {'qty': Decimal('1'), 'transport': Decimal(transport), 'other': Decimal(other)}
    pairs = []
    for i, price in enumerate(prices):
        key = 'price' + str(i);registry[key] = Decimal(price);pairs.append((key, 'qty'))
    expected = {'BEFORE': {'goods': pairs, 'transport_keys': ['transport'], 'other_logistics_keys': ['other'], 'cost_confirmed': True}}
    proposal = CalculationProposal(options=[{'option_id': 'BEFORE', 'goods': [{'value_key': k, 'quantity_key': q} for k, q in pairs],
        'transport_keys': ['transport'], 'other_logistics_keys': ['other']}])
    return evaluate(proposal, registry, expected)['BEFORE']['total_cost']


def test_shared_rounding_is_per_component_after_sum_not_per_line_or_final_total():
    from decimal import Decimal
    assert _shared_total(['0.005', '0.005']) == Decimal('0.01')  # not 0.02 per-line
    assert _shared_total(['1.004'], '0.004', '0.004') == Decimal('1.00')  # exact 1.012, final-only 1.01
    assert _shared_total(['1.006'], '0.006') == Decimal('1.02')  # exact 1.012
    assert Decimal('1.012') <= Decimal('1.015') < _shared_total(['1.006'], '0.006')


@pytest.mark.parametrize('cap,rounded_expected', [('100.00', '100.004'), ('100.005', '100.004')])
def test_rounding_changes_target_grid_and_policy_limit_decision(team_storage, cap, rounded_expected):
    from decimal import Decimal
    from app.agents.tools import compute_plan_set as build_plan_set
    from app.agents.tools import compute_counter_offer as calculate_offer, check_counter_offer_calculation as validate_offer
    _, scope, _, _, repo = team_storage
    data = repo.snapshot(scope)['data']
    data['changeable_quantity'] = '1'
    data['incumbent'].update(old_unit_price='99', requested_unit_price='102')
    data['policy'].update(allow_split=False, max_suppliers=1, allocation_step='1', min_unit_price='99',
                          max_unit_price='102', max_total_cost=cap, price_increment='0.001', skip_if_request_not_more_than_alternative=False)
    for i, q in enumerate(data['quotes']):
        q.update(unit_price='102' if i == 0 else '101', available_quantity='1',
                 costs={'transport': '0', 'other_logistics': '0', 'switching': '0'})
    bundle = build_plan_set(data);offer = calculate_offer(data, bundle)
    assert offer['offer']['target_unit_price'] == rounded_expected and validate_offer(data, bundle, offer)['valid']
    # Boundary comparison only, not an alternate production optimizer.
    grid = [Decimal(i) / 1000 for i in range(99990, 100021)]
    rounded_target = max(p for p in grid if _shared_total([str(p)]) <= Decimal(cap))
    assert rounded_target == Decimal('100.004')
    assert _shared_total([str(rounded_target + Decimal('0.001'))]) > Decimal(cap)


@pytest.mark.parametrize('feature,code', [('fixed', 'TEAM_FIXED_ALLOCATION_UNREPRESENTABLE')])
def test_blocked_features_are_explicit_holds_not_supplier_questions(team_storage, feature, code):
    common, _, supplement, refs, _ = team_storage
    if feature == 'fixed':supplement.facts['fixed_commitment']['value']['quantity'] = '1'
    supplement.refresh_binding()
    result = run_plan(team_storage)
    assert result.next_action == 'HOLD' and result.unknown_fields == [code]
    assert not refs.records


def test_existing_core_fixed_contract_still_works_but_team_mapping_is_not_claimed(team_storage):
    from app.agents.tools import compute_plan_set as build_plan_set
    from app.agents.tools import compute_counter_offer as calculate_offer, check_counter_offer_calculation as validate_offer
    _, scope, _, _, repo = team_storage
    data = repo.snapshot(scope)['data']
    data['fixed_commitment']['quantity'] = '100'
    data['changeable_quantity'] = '900'
    bundle = build_plan_set(data)
    keep = next(p for p in bundle['plans'] if p.get('type') == 'KEEP')
    assert keep['total_cost'] == '5450000'
    assert keep['lines'][0]['price_scope'] == 'FIXED_CONTRACT'
    result = calculate_offer(data, bundle)
    assert result['decision'] == 'READY' and validate_offer(data, bundle, result)['valid']


@pytest.mark.parametrize('field,original,forged', [
    ('unit_price', '15500', '5500'), ('unit_price', '55000', '5500'),
    ('unit_price', '5500.5', '5500'), ('available_quantity', '1600', '600'),
    ('moq', '100', '0'), ('previous_unit_price', '15000', '5000'),
])
@pytest.mark.parametrize('decoy', [False, True])
def test_review_numeric_source_boundaries_and_field_binding(team_storage, field, original, forged, decoy):
    common, _, supplement, refs, _ = team_storage
    row = common.quote_versions['qv-a' if field in ('unit_price', 'previous_unit_price') else 'qv-b']
    mail = common.mail_messages[row.source_mail_id]
    mail['body_text'] = mail['body_text'].replace(field + ': ' + forged, field + ': ' + original)
    if decoy:mail['body_text'] += '\nunrelated_reference: ' + forged
    supplement.refresh_binding()
    result = run_plan(team_storage)
    assert result.next_action == 'RECHECK_SOURCE' and result.unknown_fields == [field]
    assert not refs.records


def set_pack(storage, unit='kg', raw_unit=None):
    common, _, supplement, _, _ = storage
    row = common.quote_versions['qv-b']
    raw = '300' + (raw_unit or unit) + ' 단위'
    row.pricing_terms_jsonb.update(pack_multiple='300', pack_multiple_unit=unit)
    row.evidence_jsonb['pack_multiple'] = {'raw_value': raw, 'value': '300', 'quantity_unit': unit,
        'mail_id': row.source_mail_id, 'location': row.source_mail_id + ' 본문'}
    common.mail_messages[row.source_mail_id]['body_text'] += '\npack_multiple: ' + raw
    supplement.refresh_binding()


def test_review_pack_multiple_registered_allocation_and_repricing(team_storage):
    from app.agents.tools import compute_plan_set as build_plan_set
    common, scope, _, refs, repo = team_storage
    set_pack(team_storage)
    result = run_plan(team_storage)
    assert result.next_action == 'PLANS_PROPOSED'
    assert {a.quantity for p in result.plans for a in p.allocations if a.supplier_id == 'supplier-b'} == {'300', '600'}
    mapped = repo.snapshot(scope)['data']
    assert mapped['quotes'][1]['pack_multiple'] == '300'
    assert mapped['quotes'][1]['pack_multiple_unit'] == 'kg'
    bundle = build_plan_set(mapped)
    for p in bundle['plans']:
        if p['status'] != 'VALID' and any(a['supplier_id']=='supplier-b' and a['quantity'] in ('100','200','400','500') for a in p['proposal']['allocations']):
            assert p['issues'][0]['code'] == 'PACK_MULTIPLE_VIOLATION'
    AgentRunner(ToolDrivenModel(messages=iter([])), runs=common)(R.COUNTER_OFFER,
        CounterOfferIn(case_id=UUID(CASE), input_revision=1, quote_version_ids=list(scope.quote_ids), validated_plans=result.plans))
    offer = next(r['payload']['result'] for r in refs.records.values() if r['kind']=='OFFER_CALCULATION')
    assert {a['quantity'] for p in offer['comparison']['plans_at_proposed_price'] for a in p['allocations'] if a['supplier_id']=='supplier-b'} == {'300','600'}


def test_review_pack_independent_check_does_not_trust_operating_acceptance(team_storage):
    from app.agents.tools import compute_plan_set as build_plan_set
    from app.agents.tools import verify_independent_plan_cost as verify_plan_cost
    from app.agents.tools import CalculationError as ReviewError
    _, scope, _, _, repo = team_storage
    data = repo.snapshot(scope)['data']
    row = next(p for p in build_plan_set(data)['plans'] if p['status']=='VALID' and
               any(a['supplier_id']=='supplier-b' and a['quantity']=='400' for a in p['allocations']))
    data['quotes'][1].update(pack_multiple='300', pack_multiple_unit='kg', pack_multiple_source_ref='synthetic')
    with pytest.raises(ReviewError) as rejected:
        verify_plan_cost(data, row)
    assert rejected.value.issue['code'] == 'INDEPENDENT_PACK_MULTIPLE_VIOLATION'


def test_review_pack_unsupported_unit_holds(team_storage):
    set_pack(team_storage, 'box')
    result = run_plan(team_storage)
    assert result.next_action == 'HOLD' and result.unknown_fields == ['PACK_MULTIPLE_UNIT_UNSUPPORTED:qv-b']
    assert not team_storage[3].records


def test_review_revision_zero_uses_team_contract(team_storage):
    common, scope, supplement, refs, _ = team_storage
    common.work_cases[CASE]['input_revision'] = 0
    scope = Scope(CASE, 0, scope.quote_ids)
    supplement.scope = scope
    supplement.refresh_binding()
    result = run_plan(team_storage, scope=scope)
    assert result.next_action == 'PLANS_PROPOSED' and result.plans
    outcome=AgentRunner(ToolDrivenModel(messages=iter([])), runs=common)(R.COUNTER_OFFER,
        CounterOfferIn(case_id=UUID(CASE), input_revision=0, quote_version_ids=list(scope.quote_ids), validated_plans=result.plans))
    assert outcome.next_action=='HOLD' and outcome.unknown_fields==['create_counter_offer_draft']
    assert any(r['kind']=='PRICE_VALIDATION' for r in refs.records.values())
    assert all(r['scope']['revision']==0 for r in refs.records.values())


@pytest.mark.parametrize('source', ['단가: 15,500원/kg; 참고값: 5500',
    'unit_price: 15500 참고값 5500', 'unit_price: 55\n00', 'unit_price: -5500',
    'unit_price: 15500 | previous_unit_price: 5500'])
def test_review_numeric_source_cannot_borrow_other_clause_or_join_lines(team_storage, source):
    common, _, supplement, refs, _ = team_storage
    mail = common.mail_messages['mail-qv-a']
    mail['body_text'] = mail['body_text'].replace('unit_price: 5500', source)
    supplement.refresh_binding()
    result = run_plan(team_storage)
    assert result.next_action=='RECHECK_SOURCE' and result.unknown_fields==['unit_price']
    assert not refs.records


def test_review_numeric_source_accepts_exact_labelled_formatted_value(team_storage):
    common, scope, supplement, _, repo = team_storage
    row = common.quote_versions['qv-a']
    row.evidence_jsonb['unit_price']['raw_value'] = '5,500원/kg'
    common.mail_messages[row.source_mail_id]['body_text'] = common.mail_messages[row.source_mail_id]['body_text'].replace(
        'unit_price: 5500', '요청 단가: 5,500원/kg')
    supplement.refresh_binding()
    assert repo.snapshot(scope)['data']['quotes'][0]['unit_price']=='5500'
    assert run_plan(team_storage).next_action=='PLANS_PROPOSED'


@pytest.mark.parametrize('step,moq,expected', [('100','400',{'600'}), ('200','0',{'600'}), ('100','0',{'300','600'})])
def test_review_pack_moq_and_policy_grid_are_separate_constraints(team_storage, step, moq, expected):
    common, _, supplement, _, _ = team_storage
    set_pack(team_storage)
    common.work_cases[CASE]['policy']['allocation_step']=step
    row = common.quote_versions['qv-b']
    row.pricing_terms_jsonb['moq']=moq
    row.evidence_jsonb['moq'].update(raw_value=moq, value=moq)
    mail = common.mail_messages[row.source_mail_id]
    mail['body_text']=mail['body_text'].replace('moq: 0','moq: '+moq)
    supplement.refresh_binding()
    result = run_plan(team_storage)
    assert result.next_action=='PLANS_PROPOSED'
    assert {a.quantity for p in result.plans for a in p.allocations if a.supplier_id=='supplier-b'}==expected


@pytest.mark.parametrize('fault,action', [('unit_mismatch','RECHECK_SOURCE'), ('missing_unit','NEEDS_EVIDENCE'),
                                       ('substring','RECHECK_SOURCE'), ('zero','RECHECK_SOURCE')])
def test_review_pack_evidence_and_unit_are_not_inferred(team_storage, fault, action):
    common, _, supplement, refs, _ = team_storage
    set_pack(team_storage)
    row = common.quote_versions['qv-b'];entry=row.evidence_jsonb['pack_multiple']
    mail=common.mail_messages[row.source_mail_id]
    if fault=='unit_mismatch':row.pricing_terms_jsonb['pack_multiple_unit']='box'
    if fault=='missing_unit':
        row.pricing_terms_jsonb.pop('pack_multiple_unit');entry.pop('quantity_unit')
        entry['raw_value']='300'
        mail['body_text']=mail['body_text'].replace('300kg 단위','300')
    if fault=='substring':mail['body_text']=mail['body_text'].replace('300kg 단위','1300kg 단위')
    if fault=='zero':
        row.pricing_terms_jsonb['pack_multiple']='0';entry.update(raw_value='0kg 단위',value='0')
        mail['body_text']=mail['body_text'].replace('300kg 단위','0kg 단위')
    supplement.refresh_binding()
    result=run_plan(team_storage)
    assert result.next_action==action and result.unknown_fields==['pack_multiple']
    assert not refs.records


@pytest.mark.parametrize('revision', [-1, True])
def test_review_revision_contract_still_rejects_negative_and_bool(team_storage, revision):
    from app.agents.tools import validate_purchase_inputs as parse, CalculationError as ReviewError
    _, scope, _, _, repo=team_storage
    data=repo.snapshot(scope)['data'];data['revision']=revision
    with pytest.raises(ReviewError) as rejected:parse(data)
    assert rejected.value.issue['field_name']=='revision'


def test_review_revision_zero_to_one_still_blocks(team_storage):
    common, scope, supplement, refs, _ = team_storage
    common.work_cases[CASE]['input_revision']=0
    scope=Scope(CASE,0,scope.quote_ids);supplement.scope=scope;supplement.refresh_binding()
    def advance():common.work_cases[CASE]['input_revision']=1
    with pytest.raises(Exception, match='STALE_REVISION'):
        run_plan(team_storage, ToolDrivenModel(messages=iter([]),hook=advance),scope)
    assert not refs.records and common.agent_runs['run-1']['status']=='FAILED'


def test_final_guard_team_plan_repeats_without_reference_or_result_writes(team_storage, monkeypatch):
    common, scope, _, refs, repo = team_storage
    output = run_plan(team_storage)
    data = PlanIn(case_id=UUID(CASE), input_revision=scope.revision, quote_version_ids=list(scope.quote_ids))
    context = RunContext(CASE, scope.revision, 'SYNTHETIC-final-run')
    before = deepcopy((refs.records, common.agent_runs))
    forbid_final_writes(monkeypatch, repo)
    def forbidden(*args):pytest.fail('Final guard attempted a ValidationStore write')
    monkeypatch.setattr(refs, 'put_if_current', forbidden)
    for _ in range(2):
        assert validate_review_output(R.RESPONSE_PLAN, data, context, output) == output
    forged = wire(output);forged['plans'][0]['total_amount'] = '1'
    with pytest.raises(BridgeError, match='PLAN_REFERENCE_MISMATCH'):
        validate_review_output(R.RESPONSE_PLAN, data, context, forged)
    assert (refs.records, common.agent_runs) == before


@pytest.mark.parametrize('role', [R.RESPONSE_PLAN, R.COUNTER_OFFER])
def test_final_guard_team_policy_hold_requires_no_supplements_or_validation_store(team_storage, monkeypatch, role):
    common, scope, _, refs, repo = team_storage
    common.work_cases[CASE]['policy'] = None
    configure_review_stores(common)
    data_type = PlanIn if role == R.RESPONSE_PLAN else CounterOfferIn
    data = data_type(case_id=UUID(CASE), input_revision=scope.revision, quote_version_ids=list(scope.quote_ids))
    context = RunContext(CASE, scope.revision, 'SYNTHETIC-final-run')
    output = {'next_action': 'HOLD', 'reason': 'SYNTHETIC policy missing', 'unknown_fields': ['policy']}
    def forbidden(*args):pytest.fail('Normal HOLD must not load calculation-ready business facts')
    monkeypatch.setattr(ReviewAccess, 'snapshot', forbidden)
    forbid_final_writes(monkeypatch, repo)
    for _ in range(2):
        assert validate_review_output(role, data, context, output).next_action == 'HOLD'
    # Even without policy, cross-case/stale quote data cannot become a valid HOLD.
    common.quote_versions['qv-b'].work_case_id = 'foreign-case'
    # MemoryStore removes that foreign row from the case's latest quote IDs.
    with pytest.raises(BridgeError, match='STALE_QUOTE_VERSIONS'):
        validate_review_output(role, data, context, output)
    assert not refs.records and not common.agent_runs


def test_final_guard_team_proposal_requires_validation_store(team_storage):
    common, scope, supplement, _, repo = team_storage
    output = run_plan(team_storage)
    configure_review_stores(common, supplements=supplement)
    data = PlanIn(case_id=UUID(CASE), input_revision=scope.revision, quote_version_ids=list(scope.quote_ids))
    with pytest.raises(MissingTeamData) as rejected:
        validate_review_output(R.RESPONSE_PLAN, data, RunContext(CASE, scope.revision), output)
    assert rejected.value.fields == ['PlanStore.read_plan_calculation']


def test_final_guard_team_validation_row_cannot_replace_missing_shared_draft_lookup(team_storage, monkeypatch):
    common, scope, _, refs, repo = team_storage
    plan = run_plan(team_storage)
    context = RunContext(CASE, scope.revision, 'SYNTHETIC-final-run')
    service = BridgeService(repo.for_run(context), None)
    calc = service.calculate(scope, plan.plans)
    target = service.validate_target(scope, calc['target'], calc['calculation_ref'])
    # A record with matching target is not evidence of an actual common-tool draft.
    ref = refs.put_if_current(scope, digest(service.repo.snapshot(scope)), 'DRAFT',
        {'target': wire(target), 'send_authorized': False}, context.agent_run_id)
    data = CounterOfferIn(case_id=UUID(CASE), input_revision=scope.revision,
                         quote_version_ids=list(scope.quote_ids), validated_plans=plan.plans)
    payload = {'next_action': 'COUNTER_OFFER_PROPOSED', 'reason': 'SYNTHETIC',
               'target': wire(target), 'draft_ids': [ref]}
    before = deepcopy((refs.records, common.agent_runs))
    forbid_final_writes(monkeypatch, repo)
    def forbidden(*args):pytest.fail('Final guard attempted a ValidationStore write')
    monkeypatch.setattr(refs, 'put_if_current', forbidden)
    for _ in range(2):
        with pytest.raises(MissingTeamData) as rejected:
            validate_review_output(R.COUNTER_OFFER, data, context, payload)
        assert rejected.value.fields == ['create_counter_offer_draft.resolve']
    assert (refs.records, common.agent_runs) == before
