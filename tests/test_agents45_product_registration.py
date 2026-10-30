"""Only the model and storage are doubles. Normal imports register real tools."""
import ast
from copy import deepcopy
import json
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from agent_memory_store import MemoryStore
from app.agents import tools
from app.agents.agents import AgentRunner, PROMPTS
from app.agents.schemas import AgentRole as R, PlanIn, CounterOfferIn
from app.agents.kan60.team_bridge.repository import SyntheticRepository, Scope

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
            if counter:
                plans = data['validated_plans']
                if self.mode == 'drop_plan':plans = plans[:-1]
                return call('calculate_counter_offer', {'validated_plans': plans})
            # Extra forged arguments cannot override the hidden RunContext.
            return call('build_plan_candidates', {'case_id': 'forged'} if self.mode == 'spoof' else {})
        if last.name == 'build_plan_candidates':
            plans = deepcopy(result['plans'])
            if self.mode == 'fix_once' and not any(m.name == 'validate_purchase_plan' for m in history):
                plans[0]['allocations'][0]['quantity'] = '1'
            return call('validate_purchase_plan', {'plans': plans})
        if last.name == 'validate_purchase_plan':
            if result['plan_check'] == 'FIX':return call('build_plan_candidates', {})
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
def storage():
    row = json.loads((Path(__file__).parent/'fixtures/agents45_normal13.json').read_text(encoding='utf-8'))
    source = row['inputs'];case = source['case_id'];snapshot = source['snapshot']
    repo = SyntheticRepository({case: snapshot}, {case})
    scope = Scope(case, snapshot['revision'], tuple(sorted(source['quote_ids'])))
    common = MemoryStore()
    common.add_case(case, input_revision=scope.revision, item_name='SYNTHETIC', policy=snapshot['data']['policy'])
    tools.use_case_store(common)
    tools.use_review_repository(repo)
    yield repo, scope, common
    tools.use_review_repository(None)
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
    assert 'check_comparability' not in tools._registry and 'create_mail_draft' not in tools._registry
    for name in OWN:
        schema = tools._registry[name].tool_call_schema.model_json_schema()
        assert not {'case_id', 'input_revision', 'runtime', 'agent_run_id', 'quote_version_ids'} & set(schema.get('properties', {}))
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


def test_unresolved_questions_are_not_silently_ignored(storage):
    result = run_plan(storage, questions=[{'supplier_id': 's', 'field': 'unit_price',
        'priority': 'MUST_CONFIRM', 'reason': 'SYNTHETIC missing source'}])
    assert result.next_action == 'NEEDS_EVIDENCE' and result.unknown_fields == ['unit_price']


def test_revision_change_blocks_before_calculation_or_reference_writes(storage):
    repo, scope, common = storage
    model = ToolDrivenModel(messages=iter([]), hook=lambda: repo.advance_revision(scope.case_id))
    with pytest.raises(Exception, match='STALE_REVISION'):
        AgentRunner(model, runs=common)(R.RESPONSE_PLAN, plan_input(scope))
    assert not repo._records and common.agent_runs['run-1']['status'] == 'FAILED'
    assert common.agent_runs['run-1']['output_jsonb'] == {}


def test_unconfigured_repository_has_no_implicit_test_fallback(storage):
    _, scope, common = storage
    tools.use_review_repository(None)
    result = AgentRunner(ToolDrivenModel(messages=iter([])), runs=common)(R.RESPONSE_PLAN, plan_input(scope))
    assert result.next_action == 'HOLD' and result.unknown_fields == ['ReviewRepository']


def test_quote_scope_cannot_expand_beyond_authorized_input(storage):
    _, scope, common = storage
    data = plan_input(scope).model_copy(update={'quote_version_ids': ['foreign-quote']})
    with pytest.raises(Exception, match='STALE_QUOTE_VERSIONS'):
        AgentRunner(ToolDrivenModel(messages=iter([])), runs=common)(R.RESPONSE_PLAN, data)


def test_product_modules_do_not_import_evals_tests_or_reports():
    for path in (ROOT/'app/agents/kan60').rglob('*.py'):
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
    from app.agents.kan60.team_bridge.service import BridgeService, wire
    from app.agents.kan60.team_bridge.repository import BridgeError
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
