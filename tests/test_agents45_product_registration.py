"""Only the model and storage are doubles. Normal imports register real tools."""
import ast
from copy import deepcopy
import json
from pathlib import Path
from typing import Any
from types import SimpleNamespace
from uuid import UUID

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from agent_memory_store import MemoryStore, RoleMemoryStore
from app.agents import tools
from app.agents.agents import AgentRunner, PROMPTS
from app.agents.schemas import AgentRole as R, PlanIn, CounterOfferIn
from agent_memory_store import SyntheticCalculationStore as SyntheticRepository
from app.agents.tools import ExecutionScope as Scope
from app.agents.tools import OutputValidationError as BridgeError
from app.agents.tools import ReviewCalculator as BridgeService, model_payload as wire
from app.agents.agents import check_final_output
from app.agents.schemas import RunContext, PlanOut, CounterOfferOut

ROOT = Path(__file__).resolve().parents[1]
OWN = {'build_plan_candidates', 'validate_purchase_plan', 'calculate_counter_offer', 'validate_counter_offer'}


class ToolDrivenModel(GenericFakeChatModel):
    mode: str = 'normal'
    hook: Any = None

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        data = json.loads(next(m.content for m in messages if isinstance(m, HumanMessage)))
        counter = 'validated_plans' in data
        history = [m for m in messages if isinstance(m, ToolMessage)]
        def call(name, args):
            return ChatResult(generations=[ChatGeneration(message=AIMessage(content='', tool_calls=[
                {'name': name, 'args': args, 'id': 'fake-'+str(len(messages))}]))])
        def final(action, **values):
            return call('CounterOfferOut' if counter else 'PlanOut',
                        {'next_action': action, 'reason': 'SYNTHETIC model double; real registered tools', **values})
        if not history:
            return call('get_case_context', {})
        last = history[-1];result = json.loads(last.content)
        if result.get('status') in ('CASE_NOT_FOUND', 'REVISION_CHANGED', 'HOLD', 'NEEDS_EVIDENCE', 'RECHECK_SOURCE'):
            return final(result['status'] if result['status'] in ('NEEDS_EVIDENCE', 'RECHECK_SOURCE') else 'HOLD',
                         unknown_fields=result.get('unknown_fields', [result['status']]))
        if last.name == 'get_case_context':
            if self.hook:self.hook()
            return call('check_comparability', {'quote_version_ids':data['quote_version_ids']})
        if last.name == 'check_comparability':
            if counter:
                plans = data['validated_plans']
                if self.mode == 'drop_plan':plans = plans[:-1]
                return call('calculate_counter_offer', {'validated_plans': plans})
            # Extra forged arguments cannot override the hidden RunContext.
            return call('build_plan_candidates', {'quote_version_ids':data['quote_version_ids'], **({'case_id':'forged'} if self.mode=='spoof' else {})})
        if last.name == 'build_plan_candidates':
            plans = deepcopy(result['plans'])
            if self.mode == 'fix_once' and not any(m.name == 'validate_purchase_plan' for m in history):
                plans[0]['allocations'][0]['quantity'] = '1'
            return call('validate_purchase_plan', {'plans': plans})
        if last.name == 'validate_purchase_plan':
            if result['plan_check'] == 'FIX':return call('build_plan_candidates', {'quote_version_ids':data['quote_version_ids']})
            return final('PLANS_PROPOSED', plans=result['plans'])
        if last.name == 'calculate_counter_offer':
            target = deepcopy(result['target'])
            if self.mode == 'bad_target':target['target_unit_price'] = '999999'
            return call('validate_counter_offer', {'target': target, 'calculation_ref': result['calculation_ref']})
        if last.name == 'validate_counter_offer':
            assert result['valid'] is True
            missing = result['missing_dependencies']
            assert missing, 'This test does not implement or simulate the shared draft tool'
            return final('HOLD', unknown_fields=missing)
        raise AssertionError('unexpected tool step')


@pytest.fixture
def storage(monkeypatch):
    row = json.loads((Path(__file__).parent/'fixtures/agents45_normal13.json').read_text(encoding='utf-8'))
    source = row['inputs'];case = source['case_id'];snapshot = source['snapshot']
    repo = SyntheticRepository({case: snapshot}, {case})
    scope = Scope(case, snapshot['revision'], tuple(sorted(source['quote_ids'])))
    common = MemoryStore()
    common.add_case(case, input_revision=scope.revision, item_name='SYNTHETIC', policy=snapshot['data']['policy'])
    tools.use_case_store(common)
    roles = RoleMemoryStore(common)
    tools.use_role_stores(priority=roles, plans=roles, counter_offers=roles)
    # Explicit synthetic storage-access double; Runner and guard stay real.
    monkeypatch.setattr(tools, '_ReviewAccess', lambda context, role: repo)
    yield repo, scope, common
    tools.use_role_stores()
    tools.use_case_store(None)


def plan_input(scope, **kwargs):
    return PlanIn(case_id=UUID(scope.case_id), input_revision=scope.revision,
                  quote_version_ids=list(scope.quote_ids), **kwargs)


def run_plan(storage, mode='normal', **kwargs):
    repo, scope, common = storage
    return AgentRunner(ToolDrivenModel(messages=iter([]), mode=mode), runs=common)(R.RESPONSE_PLAN, plan_input(scope, **kwargs))


def test_normal_imports_register_only_four_owned_tools_and_preserve_shared_contract():
    assert OWN <= tools._registry.keys()
    assert {'get_case_context', 'read_mail_evidence', 'save_agent_result', 'store_structured_mail'} <= tools._registry.keys()
    assert {'check_comparability', 'calculate_break_even'} <= tools._registry.keys()
    assert 'create_mail_draft' not in tools._registry
    for name in OWN:
        schema = tools._registry[name].tool_call_schema.model_json_schema()
        assert not {'case_id', 'input_revision', 'runtime', 'agent_run_id'} & set(schema.get('properties', {}))
    for name in ('check_comparability','calculate_break_even','build_plan_candidates'):
        schema=tools._registry[name].tool_call_schema.model_json_schema()
        assert 'quote_version_ids' in schema['required']
        assert schema['properties']['quote_version_ids']['items']['type']=='string'
        assert not {'case_id','input_revision','agent_run_id','runtime'} & schema['properties'].keys()
    counter=tools._registry['calculate_counter_offer'].tool_call_schema.model_json_schema()
    assert counter['properties']['validated_plans']['items']['$ref'].endswith('/Plan')
    assert OWN & {t.name for t in tools.tools_for(R.RESPONSE_PLAN)} == {'build_plan_candidates', 'validate_purchase_plan'}
    assert OWN & {t.name for t in tools.tools_for(R.COUNTER_OFFER)} == {'calculate_counter_offer', 'validate_counter_offer'}
    assert not OWN & {t.name for t in tools.tools_for(R.INFO_EXTRACTOR)}
    assert not OWN & {t.name for t in tools.tools_for(R.CANDIDATE)}
    assert not OWN & {t.name for t in tools.tools_for(R.INFO_PRIORITY)}
    assert PROMPTS[R.RESPONSE_PLAN] and PROMPTS[R.COUNTER_OFFER]


@pytest.mark.parametrize('mode', ['normal', 'fix_once', 'spoof'])
def test_stage4_real_runner_real_registry_real_validation(storage, mode):
    repo, scope, common = storage
    before = dict(tools._registry)
    result = run_plan(storage, mode)
    assert result.next_action == 'PLANS_PROPOSED' and result.plans
    for plan in result.plans:
        assert repo.resolve(scope, 'PLAN_CALCULATION', plan.calculation_ref)['status'] == 'PASS'
    recorded = common.agent_runs['run-1']
    assert recorded['status'] == 'COMPLETED' and recorded['output_jsonb'] == result.model_dump(mode='json')
    assert tools._registry == before
    assert not any(r['kind'] == 'AGENT_RESULT' for r in repo._records.values())


def test_stage5_computes_and_validates_then_holds_for_real_missing_shared_draft(storage):
    repo, scope, common = storage
    plan = run_plan(storage)
    data = CounterOfferIn(case_id=UUID(scope.case_id), input_revision=scope.revision,
                         quote_version_ids=list(scope.quote_ids), validated_plans=plan.plans)
    result = AgentRunner(ToolDrivenModel(messages=iter([])), runs=common)(R.COUNTER_OFFER, data)
    validations = [r['payload'] for r in repo._records.values() if r['kind'] == 'PRICE_VALIDATION']
    assert validations and validations[-1]['status'] == 'PASS'
    assert validations[-1]['target']['target_unit_price'] == '5416'
    assert result.next_action == 'HOLD' and result.unknown_fields == ['create_mail_draft']
    assert result.target is None and result.draft_ids == []
    assert not any(r['kind'] == 'DRAFT' for r in repo._records.values())
    assert common.agent_runs['run-2']['output_jsonb'] == result.model_dump(mode='json')


@pytest.mark.parametrize('mode,code', [('drop_plan', 'COUNTER_INPUT_PLAN_SET_MISMATCH'),
                                     ('bad_target', 'TARGET_CALCULATION_MISMATCH')])
def test_stage5_rejects_missing_alternative_or_tampered_price(storage, mode, code):
    repo, scope, common = storage
    plan = run_plan(storage)
    result = AgentRunner(ToolDrivenModel(messages=iter([]), mode=mode), runs=common)(R.COUNTER_OFFER,
        CounterOfferIn(case_id=UUID(scope.case_id), input_revision=scope.revision,
                       quote_version_ids=list(scope.quote_ids), validated_plans=plan.plans))
    assert result.next_action == 'HOLD' and result.unknown_fields == [code]
    assert not any(r['kind'] == 'PRICE_VALIDATION' for r in repo._records.values())


def test_missing_policy_does_not_generate_price(storage):
    repo, scope, _ = storage
    repo._cases[scope.case_id]['data']['policy'] = None
    result = run_plan(storage)
    assert result.next_action == 'HOLD' and result.unknown_fields == ['policy']
    assert not repo._records


def test_answered_pending_question_is_not_asked_again(storage):
    result = run_plan(storage, questions=[{'supplier_id': 's', 'field': 'unit_price',
        'priority': 'MUST_CONFIRM', 'reason': 'SYNTHETIC missing source'}])
    assert result.next_action == 'PLANS_PROPOSED' and result.unknown_fields == []


def test_revision_change_blocks_before_calculation_or_reference_writes(storage):
    repo, scope, common = storage
    model = ToolDrivenModel(messages=iter([]), hook=lambda: repo.advance_revision(scope.case_id))
    with pytest.raises(Exception, match='STALE_REVISION'):
        AgentRunner(model, runs=common)(R.RESPONSE_PLAN, plan_input(scope))
    assert not repo._records and common.agent_runs['run-1']['status'] == 'FAILED'
    assert common.agent_runs['run-1']['output_jsonb'] == {}


def test_unconfigured_repository_has_no_implicit_test_fallback(storage):
    _, scope, common = storage
    tools.use_role_stores()
    result = AgentRunner(ToolDrivenModel(messages=iter([])), runs=common)(R.RESPONSE_PLAN, plan_input(scope))
    assert result.next_action == 'HOLD' and result.unknown_fields == ['RESPONSE_PLANStore']


def test_quote_scope_cannot_expand_beyond_authorized_input(storage):
    _, scope, common = storage
    data = plan_input(scope).model_copy(update={'quote_version_ids': ['foreign-quote']})
    with pytest.raises(Exception, match='STALE_QUOTE_VERSIONS'):
        AgentRunner(ToolDrivenModel(messages=iter([])), runs=common)(R.RESPONSE_PLAN, data)


def test_product_modules_do_not_import_evals_tests_or_reports():
    for path in (ROOT/'app/agents').glob('*.py'):
        parsed = ast.parse(path.read_text(encoding='utf-8-sig'))
        for node in ast.walk(parsed):
            modules = [node.module or ''] if isinstance(node, ast.ImportFrom) else [a.name for a in node.names] if isinstance(node, ast.Import) else []
            assert all(m.split('.')[0] not in ('evals', 'tests', 'reports') for m in modules), path
        assert 'installed_tools' not in path.read_text(encoding='utf-8-sig')


class DirectOutputModel(GenericFakeChatModel):
    """Bypasses every business tool; calls only the structured response schema."""
    payload: dict
    schema_name: str

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        assert not any(isinstance(m, ToolMessage) for m in messages)
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content='', tool_calls=[
            {'name': self.schema_name, 'args': self.payload, 'id': 'direct-output'}]))])


class RunnerFinalGuardMissing(AssertionError):
    pass


@pytest.mark.xfail(strict=True, raises=RunnerFinalGuardMissing,
    reason='OPEN orchestration gap: Runner does not call the role output guard before COMPLETED')
@pytest.mark.parametrize('fault', ['total', 'plan_reference', 'validation_reference', 'draft_id'])
def test_open_runner_direct_output_must_not_complete_forged_result(storage, fault):
    from app.agents.tools import ReviewCalculator as BridgeService, model_payload as wire
    from app.agents.tools import OutputValidationError as BridgeError
    repo, scope, common = storage
    baseline = run_plan(storage)
    service = BridgeService(repo, None)
    if fault in ('total', 'plan_reference'):
        role, data, schema = R.RESPONSE_PLAN, plan_input(scope), 'PlanOut'
        payload = wire(baseline)
        payload['plans'][0]['total_amount' if fault=='total' else 'calculation_ref'] = '1' if fault=='total' else 'FORGED-plan'
    else:
        role, schema = R.COUNTER_OFFER, 'CounterOfferOut'
        data = CounterOfferIn(case_id=UUID(scope.case_id), input_revision=scope.revision,
            quote_version_ids=list(scope.quote_ids), validated_plans=baseline.plans)
        calc = service.calculate(scope, baseline.plans)
        target = wire(service.validate_target(scope, calc['target'], calc['calculation_ref']))
        if fault=='validation_reference':target['validation_ref']='FORGED-validation'
        payload = {'next_action':'COUNTER_OFFER_PROPOSED', 'reason':'SYNTHETIC direct bypass', 'unknown_fields':[],
                   'target':target, 'draft_ids':['FORGED-draft']}
    # The existing owned guard rejects the same payload. It is not connected to Runner.
    with pytest.raises(BridgeError):service.check_output(scope, role, payload, for_save=True)
    try:
        AgentRunner(DirectOutputModel(messages=iter([]), payload=payload, schema_name=schema), runs=common)(role, data)
    except BridgeError:
        assert common.agent_runs['run-2']['status']=='FAILED'
        return
    saved = common.agent_runs['run-2']
    assert saved['output_jsonb'] == payload
    assert saved['status'] == 'COMPLETED'
    raise RunnerFinalGuardMissing('Known gap: forged structured output was persisted as COMPLETED')


@pytest.fixture
def final_case(storage):
    repo, scope, common = storage
    service = BridgeService(repo, None)
    plans, _, _ = service.validate_plans(scope, service.build_candidates(scope)['plans'])
    plan = PlanOut(next_action='PLANS_PROPOSED', reason='SYNTHETIC verified plans', plans=plans)
    calculation = service.calculate(scope, plans)
    target = service.validate_target(scope, calculation['target'], calculation['calculation_ref'])
    # Existing draft lookup is a storage double, never a model/mail call or an
    # implementation of the team's still-missing common draft contract.
    draft = repo.put_if_current(scope, 'DRAFT', {'target': wire(target),
        'draft': {'body': 'SYNTHETIC existing draft'}, 'send_authorized': False})
    counter = CounterOfferOut(next_action='COUNTER_OFFER_PROPOSED', reason='SYNTHETIC validated target',
                              target=target, draft_ids=[draft])
    data = CounterOfferIn(case_id=UUID(scope.case_id), input_revision=scope.revision,
                         quote_version_ids=list(scope.quote_ids), validated_plans=plans)
    return SimpleNamespace(repo=repo, scope=scope, common=common, service=service,
        plan=plan, counter=counter, data=data,
        context=RunContext(scope.case_id, scope.revision, 'SYNTHETIC-final-run'))


def forbid_final_writes(monkeypatch, repo):
    def forbidden(*args, **kwargs):
        pytest.fail('Final validation attempted a write, draft generation or validation-creation path')
    # Team repositories are frozen and for_run returns a new bound instance.
    monkeypatch.setattr(type(repo), 'put_if_current', forbidden)
    for name in ('validate_plans', 'validate_target', 'create_draft', 'save_result'):
        monkeypatch.setattr(BridgeService, name, forbidden)


@pytest.mark.parametrize('role', [R.RESPONSE_PLAN, R.COUNTER_OFFER])
def test_final_guard_repeated_valid_output_is_read_only(final_case, monkeypatch, role):
    c = final_case
    data, output = (plan_input(c.scope), c.plan) if role == R.RESPONSE_PLAN else (c.data, c.counter)
    before = deepcopy((c.repo._records, c.repo.audit, c.common.agent_runs, wire(data), wire(output)))
    forbid_final_writes(monkeypatch, c.repo)
    for _ in range(2):
        assert check_final_output(role, data, c.context, output) == output
        for for_save in (False, True):
            assert c.service.check_output(c.scope, role, output, for_save=for_save) == output
    assert (c.repo._records, c.repo.audit, c.common.agent_runs, wire(data), wire(output)) == before


@pytest.mark.parametrize('fault', [
    'total', 'allocation', 'plan_ref', 'missing_plan_ref', 'missing_plan',
    'price', 'supplier', 'conditions', 'basis', 'validation_ref', 'missing_validation_ref',
    'alternative_ref', 'policy_ref', 'draft_ref', 'missing_draft', 'input_alternative',
])
def test_final_guard_rejects_forged_or_incomplete_proposal_without_writes(final_case, monkeypatch, fault):
    c = final_case
    role, data, payload = R.COUNTER_OFFER, c.data, wire(c.counter)
    if fault in ('total', 'allocation', 'plan_ref', 'missing_plan_ref', 'missing_plan'):
        role, data, payload = R.RESPONSE_PLAN, plan_input(c.scope), wire(c.plan)
        if fault == 'total':payload['plans'][0]['total_amount'] = '1'
        if fault == 'allocation':payload['plans'][0]['allocations'][0]['quantity'] = '1'
        if fault == 'plan_ref':payload['plans'][0]['calculation_ref'] = 'FORGED'
        if fault == 'missing_plan_ref':payload['plans'][0]['calculation_ref'] = None
        if fault == 'missing_plan':payload['plans'].pop()
    else:
        field, value = {
            'price': ('target_unit_price', '1'), 'supplier': ('supplier_id', 'foreign'),
            'conditions': ('conditions', []), 'basis': ('basis_quote_version_ids', ['foreign']),
            'validation_ref': ('validation_ref', 'FORGED'), 'missing_validation_ref': ('validation_ref', None),
            'alternative_ref': ('alternative_plan_ref', 'FORGED'), 'policy_ref': ('policy_ref', 'FORGED'),
        }.get(fault, (None, None))
        if field:payload['target'][field] = value
        if fault == 'draft_ref':payload['draft_ids'] = ['FORGED']
        if fault == 'missing_draft':payload['draft_ids'] = []
        if fault == 'input_alternative':data = data.model_copy(update={'validated_plans': data.validated_plans[:-1]})
    before = deepcopy((c.repo._records, c.repo.audit))
    forbid_final_writes(monkeypatch, c.repo)
    for _ in range(2):
        with pytest.raises(BridgeError):check_final_output(role, data, c.context, payload)
    assert (c.repo._records, c.repo.audit) == before


@pytest.mark.parametrize('fault', ['target_binding', 'send_authorized', 'missing_binding'])
def test_final_guard_requires_actual_draft_target_and_unapproved_send(final_case, monkeypatch, fault):
    c = final_case
    draft = c.repo.resolve_draft(c.scope, c.counter.draft_ids[0])
    if fault == 'target_binding':draft['target']['validation_ref'] = 'OTHER-validation'
    if fault == 'send_authorized':draft['send_authorized'] = True
    if fault == 'missing_binding':draft.pop('target')
    ref = c.repo.put_if_current(c.scope, 'DRAFT', draft)
    output = c.counter.model_copy(update={'draft_ids': [ref]})
    forbid_final_writes(monkeypatch, c.repo)
    with pytest.raises(BridgeError, match='DRAFT_REFERENCE_MISMATCH'):
        check_final_output(R.COUNTER_OFFER, c.data, c.context, output)


@pytest.mark.parametrize('capability,role,code', [
    ('resolve', R.RESPONSE_PLAN, 'VALIDATION_LOOKUP_REQUIRED'),
    ('resolve', R.COUNTER_OFFER, 'VALIDATION_LOOKUP_REQUIRED'),
    ('resolve_draft', R.COUNTER_OFFER, 'DRAFT_LOOKUP_REQUIRED'),
    ('check_scope', R.RESPONSE_PLAN, 'SCOPE_CHECK_REQUIRED'),
])
def test_final_guard_rejects_missing_read_capability(final_case, monkeypatch, capability, role, code):
    c = final_case
    monkeypatch.setattr(c.repo, capability, None)
    forbid_final_writes(monkeypatch, c.repo)
    data, output = (plan_input(c.scope), c.plan) if role == R.RESPONSE_PLAN else (c.data, c.counter)
    with pytest.raises(BridgeError, match=code):check_final_output(role, data, c.context, output)


@pytest.mark.parametrize('action', ['HOLD', 'NEEDS_EVIDENCE', 'RECHECK_SOURCE'])
@pytest.mark.parametrize('role', [R.RESPONSE_PLAN, R.COUNTER_OFFER])
def test_final_guard_valid_nonproposal_needs_no_policy_or_business_snapshot(storage, monkeypatch, action, role):
    repo, scope, common = storage
    repo._cases[scope.case_id]['data']['policy'] = None
    def no_snapshot(*args):pytest.fail('Non-proposal must not require calculation-ready facts')
    monkeypatch.setattr(repo, 'snapshot', no_snapshot)
    forbid_final_writes(monkeypatch, repo)
    data = plan_input(scope) if role == R.RESPONSE_PLAN else CounterOfferIn(
        case_id=UUID(scope.case_id), input_revision=scope.revision, quote_version_ids=list(scope.quote_ids))
    output = {'next_action': action, 'reason': 'SYNTHETIC unresolved facts',
              'unknown_fields': ['policy' if action == 'HOLD' else 'unit_price']}
    for _ in range(2):
        checked = check_final_output(role, data, RunContext(scope.case_id, scope.revision), output)
        assert checked.next_action == action
    assert not repo._records and not repo.audit and not common.agent_runs


@pytest.mark.parametrize('role', [R.RESPONSE_PLAN, R.COUNTER_OFFER])
@pytest.mark.parametrize('fault', ['context_case', 'context_revision', 'quote_scope', 'stale', 'foreign_case'])
def test_final_guard_nonproposal_does_not_bypass_trusted_scope(storage, monkeypatch, role, fault):
    repo, scope, _ = storage
    data = plan_input(scope) if role == R.RESPONSE_PLAN else CounterOfferIn(
        case_id=UUID(scope.case_id), input_revision=scope.revision, quote_version_ids=list(scope.quote_ids))
    context = RunContext(scope.case_id, scope.revision)
    if fault == 'context_case':context = RunContext('foreign', scope.revision)
    if fault == 'context_revision':context = RunContext(scope.case_id, scope.revision + 1)
    if fault == 'quote_scope':data = data.model_copy(update={'quote_version_ids': ['foreign']})
    if fault == 'stale':repo.advance_revision(scope.case_id)
    if fault == 'foreign_case':
        foreign = '00000000-0000-0000-0000-000000000001'
        data = data.model_copy(update={'case_id': UUID(foreign)})
        context = RunContext(foreign, scope.revision)
    forbid_final_writes(monkeypatch, repo)
    with pytest.raises(BridgeError):
        check_final_output(role, data, context, {'next_action': 'HOLD', 'reason': 'SYNTHETIC policy missing'})


@pytest.mark.parametrize('action,fields,code', [
    ('NEEDS_EVIDENCE', [], 'UNRESOLVED_FIELDS_REQUIRED'),
    ('RECHECK_SOURCE', [], 'UNRESOLVED_FIELDS_REQUIRED'),
    ('RECHECK_SOURCE', ['policy'], 'RECHECK_FIELDS_INVALID'),
    ('NEEDS_EVIDENCE', ['create_mail_draft'], 'INTERNAL_FIELDS_REQUIRE_HOLD'),
    ('HOLD', [''], 'UNRESOLVED_FIELDS_INVALID'),
])
def test_final_guard_checks_nonproposal_field_contract(storage, action, fields, code):
    _, scope, _ = storage
    with pytest.raises(BridgeError, match=code):
        check_final_output(R.RESPONSE_PLAN, plan_input(scope), RunContext(scope.case_id, scope.revision),
            {'next_action': action, 'reason': 'SYNTHETIC', 'unknown_fields': fields})


@pytest.mark.parametrize('role', [R.RESPONSE_PLAN, R.COUNTER_OFFER])
def test_final_guard_rejects_proposal_content_in_hold(final_case, role):
    c = final_case
    data, payload = (plan_input(c.scope), wire(c.plan)) if role == R.RESPONSE_PLAN else (c.data, wire(c.counter))
    payload['next_action'] = 'HOLD'
    with pytest.raises(BridgeError, match='NON_PROPOSAL_HAS_'):
        check_final_output(role, data, c.context, payload)


def test_final_guard_catches_revision_change_during_draft_read(final_case, monkeypatch):
    c = final_case
    original = c.repo.resolve_draft
    def changed(scope, ref):
        result = original(scope, ref)
        c.repo.advance_revision(scope.case_id)
        return result
    monkeypatch.setattr(c.repo, 'resolve_draft', changed)
    forbid_final_writes(monkeypatch, c.repo)
    with pytest.raises(BridgeError, match='STALE_REVISION'):
        check_final_output(R.COUNTER_OFFER, c.data, c.context, c.counter)
