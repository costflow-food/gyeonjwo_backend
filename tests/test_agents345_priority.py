"""Real Runner/registered tools/team graph. Only model/storage are explicit doubles."""
from copy import deepcopy
from decimal import Decimal
import json
from uuid import UUID

import pytest
from langchain.tools import ToolRuntime
from langchain_core.messages import HumanMessage, ToolMessage, AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import tool

from app.agents import tools
from app.agents.agents import AgentRunner, check_final_output
from app.agents.graph import build_graph
from app.agents.nodes import Nodes, plan_validation, price_validation
from app.agents.schemas import (AgentRole as R, PriorityIn, PlanIn, CounterOfferIn, RunContext,
                                Question, QuestionPriority, PlanCheck, PriceCheck)
from app.agents.tools import OutputValidationError, ExecutionScope
from test_agents45_team_data import team_storage, CASE, run_plan
from test_agents45_product_registration import ToolDrivenModel


class PriorityModel(ToolDrivenModel):
    extraction_args: list[dict] = []

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        data = json.loads(next(m.content for m in messages if isinstance(m, HumanMessage)))
        if 'pending_questions' not in data and 'mail_ids' not in data:
            return super()._generate(messages, stop, run_manager, **kwargs)
        history = [m for m in messages if isinstance(m, ToolMessage)]
        def call(name, args):
            return ChatResult(generations=[ChatGeneration(message=AIMessage(content='', tool_calls=[
                {'name': name, 'args': args, 'id': 'synthetic-'+str(len(messages))}]))])
        def final(action, **fields):
            return call('ExtractionOut' if 'mail_ids' in data else 'PriorityOut',
                        {'next_action': action, 'reason': 'SYNTHETIC tool-driven offline model', **fields})
        if not history:return call('get_case_context', {})
        if 'mail_ids' in data:
            stored = [json.loads(m.content) for m in history if m.name == 'store_structured_mail']
            if len(stored) < len(self.extraction_args):
                return call('store_structured_mail', self.extraction_args[len(stored)])
            assert all(r['status'] == 'STORED' for r in stored), stored
            return final('STORED', quote_version_ids=[r['quote_version_id'] for r in stored])
        if history[-1].name == 'get_case_context':return call('check_comparability', {'quote_version_ids':data['quote_version_ids']})
        report = json.loads(next(m.content for m in history if m.name == 'check_comparability'))
        action = report['next_action']
        if action == 'BUILD_PLANS':return final(action, questions=[], draft_ids=[])
        if action == 'REQUEST_CLARIFICATION' or tools.LOW_VALUE_PLAN_CONTRACT_REQUIRED in report.get('missing_dependencies',[]):
            calculations=[json.loads(m.content) for m in history if m.name=='calculate_break_even']
            cost_questions=[q for q in report['questions'] if q['field']=='cost_terms']
            if len(calculations)<len(cost_questions):
                question_qid=cost_questions[len(calculations)]['evidence_refs'][0]
                # Explicit fixture model: supplier-b is the current alternative.
                alternative_qid=next(q['quote_version_id'] for q in report['quotes'] if q['supplier_id']=='supplier-b')
                return call('calculate_break_even',{'quote_version_ids':data['quote_version_ids'],
                    'alternative_quote_version_id':alternative_qid,'question_quote_version_id':question_qid})
            for result in calculations:
                if result['status']=='HOLD':return final('HOLD',unknown_fields=result['unknown_fields'])
                if result['status']=='STORED_CONDITIONAL':
                    question=result['question']
                    for collection in [report['questions'],*[g['questions'] for g in report['question_groups']]]:
                        for i,q in enumerate(collection):
                            if (q['supplier_id'],q['field'])==(question['supplier_id'],question['field']):collection[i]=question
            groups = report['question_groups']
            drafts = [json.loads(m.content)['draft_id'] for m in history if m.name == 'create_mail_draft']
            if len(drafts) < len(groups):
                group = groups[len(drafts)]
                return call('create_mail_draft', {'purpose':'CLARIFICATION', **group})
            return final(action, questions=report['questions'], draft_ids=drafts, unknown_fields=report['unknown_fields'])
        return final(action, questions=report.get('questions', []), draft_ids=[],
                     unknown_fields=report['unknown_fields'] + report.get('missing_dependencies', []))


def run_priority(storage, *, pending=()):
    common, scope, _, _, _ = storage
    return AgentRunner(PriorityModel(messages=iter([])), runs=common)(R.INFO_PRIORITY,
        PriorityIn(case_id=UUID(CASE), input_revision=scope.revision,
                   quote_version_ids=list(scope.quote_ids), pending_questions=list(pending)))


def absent_field(storage, field, qid='qv-b', *, remove_source=True):
    common, _, facts, _, _ = storage
    row = common.quote_versions[qid]
    entry = row.evidence_jsonb.pop(field, None)
    if hasattr(row, field):setattr(row, field, None)
    elif field in row.checks_jsonb:row.checks_jsonb.pop(field)
    else:row.pricing_terms_jsonb.pop(field, None)
    if remove_source and entry:
        common.mail_messages[row.source_mail_id]['body_text'] = common.mail_messages[row.source_mail_id]['body_text'].replace(
            field + ': ' + entry['raw_value'], field + ': 미확인')
    facts.refresh_binding()


def missing_shipping(storage, qid='qv-b'):
    common, _, facts, _, _ = storage
    row = common.quote_versions[qid]
    removed = next(c for c in row.cost_terms_jsonb if c['cost_type'] == 'SHIPPING')
    row.cost_terms_jsonb = [c for c in row.cost_terms_jsonb if c['cost_type'] != 'SHIPPING']
    common.mail_messages[row.source_mail_id]['body_text'] = common.mail_messages[row.source_mail_id]['body_text'].replace(
        removed['source_text'], 'SHIPPING: 미확인')
    row.shipping_cost = None
    facts.refresh_binding()


def full_volume(storage):
    common, _, facts, _, _ = storage
    row = common.quote_versions['qv-b']
    row.available_quantity = '1000'
    row.evidence_jsonb['available_quantity'].update(value='1000', raw_value='1000')
    common.mail_messages[row.source_mail_id]['body_text'] = common.mail_messages[row.source_mail_id]['body_text'].replace(
        'available_quantity: 600', 'available_quantity: 1000')
    facts.refresh_binding()


def install_clarification_storage_double(storage, monkeypatch):
    common = storage[0];saved = {}
    @tool
    def create_mail_draft(purpose: str, supplier_id: str, questions: list[Question], runtime: ToolRuntime[RunContext]) -> dict:
        """SYNTHETIC storage-only common draft contract double; never sends mail."""
        context = runtime.context
        assert purpose == 'CLARIFICATION'
        assert common.current_input_revision(context.case_id) == context.input_revision
        assert questions and all(q.supplier_id == supplier_id for q in questions)
        ref = 'SYNTHETIC-draft-' + str(len(saved) + 1)
        saved[ref] = {'purpose':purpose,'case_id':context.case_id,'input_revision':context.input_revision,'supplier_id':supplier_id,
                      'questions':[q.model_dump(mode='json') for q in questions], 'send_authorized':False}
        return {'draft_id':ref, 'send_authorized':False}
    monkeypatch.setitem(tools._registry, 'create_mail_draft', create_mail_draft)
    class ClarificationLookup:
        def read_clarification_draft(self,context,scope,ref):
            return deepcopy(saved[ref])
    monkeypatch.setattr(tools,'_draft_lookup_store',ClarificationLookup())
    return saved


def graph_state(storage, *, start='PLAN'):
    _, scope, _, _, _ = storage
    return {'case_id':CASE,'input_revision':scope.revision,'start_from':start,
            'quote_version_ids':list(scope.quote_ids),'mail_ids':[], 'role_calls':0,
            'draft_ids':[], 'unresolved':[], 'steps':[]}


def team_graph(storage, model=None):
    common = storage[0]
    return build_graph(AgentRunner(model or PriorityModel(messages=iter([])), runs=common),
                       common.current_input_revision, Nodes(preprocess=lambda state: {}))


def test_priority_uses_registered_shared_tools_and_team_outputs(team_storage):
    result = run_priority(team_storage, pending=['unit_price', 'unit_price'])
    assert result.next_action == 'BUILD_PLANS' and result.questions == [] and result.draft_ids == []
    common = team_storage[0]
    assert common.agent_runs['run-1']['input_snapshot_jsonb']['pending_questions'] == ['unit_price', 'unit_price']
    for role in (R.INFO_PRIORITY,R.RESPONSE_PLAN,R.COUNTER_OFFER):
        assert 'check_comparability' in {t.name for t in tools.tools_for(role)}
    assert 'calculate_break_even' not in {t.name for t in tools.tools_for(R.RESPONSE_PLAN)}


def test_missing_required_fields_group_by_supplier_and_no_fake_draft(team_storage, monkeypatch):
    absent_field(team_storage, 'unit_price')
    absent_field(team_storage, 'available_quantity')
    result = run_priority(team_storage, pending=['unit_price','unit_price'])
    assert result.next_action == 'HOLD' and result.draft_ids == []
    assert 'create_mail_draft' in result.unknown_fields
    assert {(q.supplier_id,q.field) for q in result.questions} == {('supplier-b','unit_price'),('supplier-b','available_quantity')}
    assert all(q.priority == 'MUST_CONFIRM' for q in result.questions)
    saved = install_clarification_storage_double(team_storage, monkeypatch)
    result = run_priority(team_storage)
    assert result.next_action == 'REQUEST_CLARIFICATION' and len(result.draft_ids) == 1
    assert saved[result.draft_ids[0]]['questions'] == [q.model_dump(mode='json') for q in result.questions]


@pytest.mark.parametrize('field', ['unit_price','available_quantity','specification'])
def test_answer_present_in_source_returns_to_extraction_without_reasking(team_storage, field):
    absent_field(team_storage, field, remove_source=False)
    result = run_priority(team_storage)
    assert result.next_action == 'RECHECK_SOURCE' and field in result.unknown_fields
    assert not result.questions and not result.draft_ids


def test_unknown_cost_and_confirmed_zero_are_distinct(team_storage):
    common, scope, _, _, repo = team_storage
    before = tools.inspect_comparability(repo, scope)
    assert before['status'] == 'PASS'
    assert next(q for q in before['quotes'] if q['quote_version_id']=='qv-a')['confirmed']['SHIPPING'] == '0'
    missing_shipping(team_storage)
    after = tools.inspect_comparability(repo, scope)
    assert after['status'] == 'NEEDS_EVIDENCE'
    assert next(q for q in after['quotes'] if q['quote_version_id']=='qv-b')['confirmed']['SHIPPING'] is None
    assert run_plan(team_storage).next_action == 'NEEDS_EVIDENCE'


def test_break_even_uses_shared_rounding_and_exclusive_half_cent_boundary(team_storage):
    full_volume(team_storage);missing_shipping(team_storage)
    _, scope, _, refs, repo = team_storage
    result = tools.compute_break_even(repo, scope, 'qv-b')
    assert result['status'] == 'CALCULATED' and Decimal(result['break_even_amount']) == Decimal('100000')
    assert result['quantity_unit'] == 'kg' and result['period'] == {'from':'2026-11-20','to':'2026-11-20'}
    assert Decimal(result['raw_upper_exclusive']) == Decimal('100000.005')
    for raw, permitted in [('100000.0049',True),('100000.005',False),('99999.995',True)]:
        assert (tools.money(Decimal(raw)) <= Decimal(result['rounded_component_limit'])) is permitted
    assert result['calculation_ref'] is None and not refs.records


@pytest.mark.parametrize('fault', ['quantity','price','policy','unit','two_costs','expired','foreign_quote','fixed'])
def test_break_even_never_invents_missing_basis(team_storage, fault):
    full_volume(team_storage);missing_shipping(team_storage)
    common, scope, facts, refs, repo = team_storage
    if fault == 'quantity':common.work_cases[CASE]['quantity'] = None
    if fault == 'price':absent_field(team_storage, 'unit_price')
    if fault == 'policy':common.work_cases[CASE]['policy'] = None
    if fault == 'unit':absent_field(team_storage, 'unit')
    if fault == 'fixed':facts.facts['fixed_commitment']['value']['quantity']='1'
    if fault == 'two_costs':
        row = common.quote_versions['qv-b']
        cost = row.cost_terms_jsonb.pop()
        common.mail_messages[row.source_mail_id]['body_text'] = common.mail_messages[row.source_mail_id]['body_text'].replace(cost['source_text'],'HANDLING: 미확인')
    if fault == 'expired':
        row = common.quote_versions['qv-b'];row.valid_until='2026-10-01'
        row.evidence_jsonb['valid_until'].update(value=row.valid_until,raw_value=row.valid_until)
        common.mail_messages[row.source_mail_id]['body_text'] = common.mail_messages[row.source_mail_id]['body_text'].replace('valid_until: 2026-11-30','valid_until: 2026-10-01')
    facts.refresh_binding()
    if fault == 'foreign_quote':
        with pytest.raises(OutputValidationError,match='TOOL_SCOPE'):tools.compute_break_even(repo,scope,'foreign')
    else:
        result=tools.compute_break_even(repo,scope,'qv-b')
        assert result['status']=='NOT_CALCULABLE' and result['break_even_amount'] is None and result['missing_inputs']
    assert not refs.records


def test_required_condition_cannot_be_offset_by_preferences_or_low_price(team_storage):
    common,scope,facts,_,repo=team_storage
    common.work_cases[CASE]['mandatory_conditions']={'origin':'US'}
    common.work_cases[CASE]['preference_score']='999999'
    facts.refresh_binding()
    report=tools.inspect_comparability(repo,scope)
    assert report['status']=='HOLD' and 'mandatory_conditions' in report['unknown_fields']
    assert all(any(c['required'] and c['state']=='FAIL' for c in q['mandatory_checks']) for q in report['quotes'])
    assert run_priority(team_storage).next_action=='HOLD'


def test_policy_missing_is_hold_and_scope_still_checked(team_storage):
    common,scope,facts,_,_=team_storage
    common.work_cases[CASE]['policy']=None;facts.refresh_binding()
    assert run_priority(team_storage).unknown_fields==['policy']
    common.quote_versions['qv-b'].work_case_id='foreign'
    with pytest.raises(OutputValidationError,match='STALE_QUOTE_VERSIONS'):
        run_priority(team_storage)


def test_real_graph_plan_to_priority_waits_only_for_stored_draft(team_storage,monkeypatch):
    missing_shipping(team_storage)
    saved=install_clarification_storage_double(team_storage,monkeypatch)
    state=team_graph(team_storage).invoke(graph_state(team_storage))
    assert [s.role for s in state['steps']]==[R.RESPONSE_PLAN,R.INFO_PRIORITY]
    assert state['status']=='WAITING_REPLY' and state['draft_ids'] and all(r in saved for r in state['draft_ids'])
    common=team_storage[0]
    assert common.agent_runs['run-2']['input_snapshot_jsonb']['pending_questions']==['cost_terms']


def test_real_graph_normal_plan_counter_stops_at_actual_draft_dependency(team_storage):
    state=team_graph(team_storage).invoke(graph_state(team_storage))
    assert [s.role for s in state['steps']]==[R.RESPONSE_PLAN,R.COUNTER_OFFER]
    assert state['status']=='ON_HOLD' and 'create_mail_draft' in state['unresolved']
    assert 'card_id' not in state and state['draft_ids']==[]


def test_verification_nodes_use_real_refs_reject_forgery_and_never_write(team_storage,monkeypatch):
    common,scope,_,refs,repo=team_storage
    plan=run_plan(team_storage)
    state={**graph_state(team_storage),'plans':plan.plans}
    before=deepcopy(refs.records)
    def forbidden(*args):pytest.fail('Node wrote a reference')
    monkeypatch.setattr(refs,'put_if_current',forbidden)
    assert plan_validation(state)['plan_check']==PlanCheck.PASS
    assert plan_validation(state)['plan_check']==PlanCheck.PASS
    forged=deepcopy(plan.plans);forged[0].total_amount='1'
    assert plan_validation({**state,'plans':forged})['plan_check']==PlanCheck.FIX
    assert price_validation({**state,'last_action':'COUNTER_OFFER_SKIPPED'})['unresolved']==['SKIPPED_EVIDENCE_REQUIRED']
    assert refs.records==before


def extraction_model(storage, monkeypatch):
    """Real extraction tool; the missing review-facts DB adapter is a named double."""
    common, _, facts, _, _ = storage
    arguments = []
    for qid in ('qv-a', 'qv-b'):
        row = common.quote_versions[qid]
        entries = row.mail_normalized[row.source_mail_id]['fields']
        evidence = {'mail_id':row.source_mail_id,'part':'body','page':None}
        arguments.append({'fields':[{'field_name':k,'raw_value':v['raw_value'],'evidence':evidence}
                                    for k,v in entries.items()],
            'cost_terms':[{k:v for k,v in c.items() if k not in ('mail_id','location')} | {'evidence':evidence}
                          for c in row.cost_terms_jsonb]})
    original = common.insert_quote_version
    def insert_and_bind_facts(record, expected_revision):
        previous = common.latest_quote_version(CASE, record.supplier_id)[0]
        result = original(record, expected_revision)
        if result:
            for key in list(facts.facts):
                if key.startswith(previous + '.'):
                    facts.facts[result[0] + key[len(previous):]] = facts.facts.pop(key)
            facts.scope = ExecutionScope(CASE, expected_revision,
                tuple(sorted(common.get_work_case(CASE)['latest_quote_version_ids'])))
            facts.record_digest = tools.review_record_digest(common.get_work_case(CASE),
                {qid:common.quote_versions[qid] for qid in facts.scope.quote_ids})
        return result
    monkeypatch.setattr(common,'insert_quote_version',insert_and_bind_facts)
    return PriorityModel(messages=iter([]),extraction_args=arguments)


def test_actual_extraction_priority_plan_counter_graph_path(team_storage,monkeypatch):
    model = extraction_model(team_storage,monkeypatch)
    state = team_graph(team_storage,model).invoke({**graph_state(team_storage,start='REPLY'),
                                                 'mail_ids':['mail-qv-a','mail-qv-b']})
    assert [s.role for s in state['steps']]==[R.INFO_EXTRACTOR,R.INFO_PRIORITY,R.RESPONSE_PLAN,R.COUNTER_OFFER]
    assert state['status']=='ON_HOLD' and state['plans'] and state['draft_ids']==[]
    assert 'create_mail_draft' in state['unresolved']


def test_source_mismatch_returns_to_real_extractor(team_storage,monkeypatch):
    model = extraction_model(team_storage,monkeypatch)
    absent_field(team_storage,'unit_price',remove_source=False)
    state=team_graph(team_storage,model).invoke(graph_state(team_storage))
    assert state['status'] != 'FAILED', [r['error_jsonb'] for r in team_storage[0].agent_runs.values()]
    assert [s.role for s in state['steps']]==[R.RESPONSE_PLAN,R.INFO_EXTRACTOR,R.INFO_PRIORITY,
                                           R.RESPONSE_PLAN,R.COUNTER_OFFER]
    assert state['steps'][0].next_action=='RECHECK_SOURCE'
    assert state['status']=='ON_HOLD' and state['plans']


def test_reply_new_revision_reenters_and_old_reference_is_rejected(team_storage,monkeypatch):
    common,scope,facts,refs,repo=team_storage
    previous=run_plan(team_storage)
    original_row=deepcopy(common.quote_versions['qv-b'])
    original_text=common.mail_messages['mail-qv-b']['body_text']
    missing_shipping(team_storage)
    install_clarification_storage_double(team_storage,monkeypatch)
    waiting=team_graph(team_storage).invoke(graph_state(team_storage))
    assert waiting['status']=='WAITING_REPLY'
    common.work_cases[CASE]['input_revision']=2
    common.quote_versions['qv-b']=original_row
    common.mail_messages['mail-qv-b']['body_text']=original_text
    facts.scope=ExecutionScope(CASE,2,scope.quote_ids);facts.refresh_binding()
    with pytest.raises(OutputValidationError,match='STALE_REVISION'):
        check_final_output(R.RESPONSE_PLAN,PlanIn(case_id=CASE,input_revision=1,
            quote_version_ids=list(scope.quote_ids)),RunContext(CASE,1),previous)
    model=extraction_model(team_storage,monkeypatch)
    state=team_graph(team_storage,model).invoke({**graph_state(team_storage,start='REPLY'),
        'input_revision':2,'mail_ids':['mail-qv-a','mail-qv-b']})
    assert [s.role for s in state['steps']]==[R.INFO_EXTRACTOR,R.INFO_PRIORITY,R.RESPONSE_PLAN,R.COUNTER_OFFER]
    assert state['status']=='ON_HOLD' and 'create_mail_draft.validation_binding' in state['unresolved']
    new_input=PlanIn(case_id=CASE,input_revision=2,quote_version_ids=state['quote_version_ids'])
    with pytest.raises(OutputValidationError):
        check_final_output(R.RESPONSE_PLAN,new_input,RunContext(CASE,2),previous)


def test_registered_sensitivity_reads_typed_run_not_conversation(team_storage):
    from types import SimpleNamespace
    common,scope,_,_,_=team_storage
    full_volume(team_storage);missing_shipping(team_storage)
    data=PriorityIn(case_id=CASE,input_revision=1,quote_version_ids=list(scope.quote_ids))
    run=common.start_agent_run(CASE,R.INFO_PRIORITY,1,data.model_dump(mode='json'),'SYNTHETIC','test')
    class Runtime(SimpleNamespace):
        @property
        def state(self):pytest.fail('Production tool read conversation state')
    runtime=Runtime(context=RunContext(CASE,1,agent_run_id=run['agent_run_id']),
                    config={'metadata':{'lc_agent_name':'info_priority'}})
    result=tools.calculate_break_even.func(list(scope.quote_ids),'qv-b',runtime)
    assert result['status']=='STORED_CONDITIONAL' and result['calculation_ref']
    runtime.context=RunContext(CASE,1,agent_run_id='forged')
    with pytest.raises(OutputValidationError,match='TRUSTED_AGENT_INPUT_REQUIRED'):
        tools.calculate_break_even.func(list(scope.quote_ids),'qv-b',runtime)


def test_counter_missing_evidence_routes_to_priority_with_actual_registered_tools(team_storage,monkeypatch):
    from typing import Any
    class LateEvidenceModel(PriorityModel):
        before_counter: Any
        changed: bool = False
        def _generate(self,messages,**kwargs):
            data=json.loads(next(m.content for m in messages if isinstance(m,HumanMessage)))
            if 'validated_plans' in data and not self.changed:
                self.changed=True
                self.before_counter()
            return super()._generate(messages,**kwargs)
    # Explicit store fault injection: evidence disappears after the plan node.
    # The real fifth-role gate must not compute against the now-unconfirmed cost.
    model=LateEvidenceModel(messages=iter([]),before_counter=lambda:missing_shipping(team_storage))
    saved=install_clarification_storage_double(team_storage,monkeypatch)
    state=team_graph(team_storage,model).invoke(graph_state(team_storage))
    assert [s.role for s in state['steps']]==[R.RESPONSE_PLAN,R.COUNTER_OFFER,R.INFO_PRIORITY]
    assert state['steps'][1].next_action=='NEEDS_EVIDENCE'
    assert state['status']=='WAITING_REPLY' and all(ref in saved for ref in state['draft_ids'])
    assert not any(row['kind']=='OFFER_CALCULATION' for row in team_storage[3].records.values())


def test_missing_fact_evidence_is_hold_without_fabricating_source(team_storage):
    team_storage[2].facts['period'].pop('evidence')
    team_storage[2].refresh_binding()
    result=run_priority(team_storage)
    assert result.next_action=='HOLD' and 'review_facts.period.evidence' in result.unknown_fields


def test_natural_language_source_answer_is_rechecked_not_reasked(team_storage):
    absent_field(team_storage,'unit_price',remove_source=False)
    mail=team_storage[0].mail_messages['mail-qv-b']
    mail['body_text']=mail['body_text'].replace('unit_price: 5400','단가는 5400원입니다.')
    result=run_priority(team_storage)
    assert result.next_action=='RECHECK_SOURCE' and not result.questions


def test_independent_cost_check_rejects_wrong_shared_evaluator(team_storage,monkeypatch):
    data=team_storage[4].snapshot(team_storage[1])['data']
    original=tools.evaluate
    def wrong(*args,**kwargs):
        result=original(*args,**kwargs)
        result['BEFORE']['total_cost']+=Decimal('1')
        return result
    monkeypatch.setattr(tools,'evaluate',wrong)
    bundle=tools.compute_plan_set(data)
    row=next(p for p in bundle['plans'] if p['status']=='VALID')
    with pytest.raises(tools.CalculationError,match='총액'):
        tools.verify_independent_plan_cost(data,row)


def test_price_node_reads_actual_reference_and_explicit_draft_store_double(team_storage,monkeypatch):
    from app.agents.schemas import DraftBinding
    common,scope,_,refs,repo=team_storage
    plan=run_plan(team_storage)
    data=CounterOfferIn(case_id=CASE,input_revision=scope.revision,
                       quote_version_ids=list(scope.quote_ids),validated_plans=plan.plans)
    run=common.start_agent_run(CASE,R.COUNTER_OFFER,scope.revision,data.model_dump(mode='json'),'SYNTHETIC','test')
    service=tools.ReviewCalculator(repo.for_run(RunContext(CASE,scope.revision,run['agent_run_id'])),None)
    calc=service.calculate(scope,plan.plans)
    target=service.validate_target(scope,calc['target'],calc['calculation_ref'])
    # A separate, stored common-draft double. No fake ID returned by product code.
    stored={'SYNTHETIC-node-draft':DraftBinding(target=target,send_authorized=False)}
    class DraftStoreDouble:
        def read_counter_offer_draft(self,context,requested_scope,ref):
            assert context.case_id==scope.case_id and requested_scope==scope
            return stored[ref]
    monkeypatch.setattr(tools,'_draft_lookup_store',DraftStoreDouble())
    state={**graph_state(team_storage),'plans':plan.plans,'counter_offer':target,
           'draft_ids':list(stored),'last_action':'COUNTER_OFFER_PROPOSED'}
    before=deepcopy((refs.records,stored,common.agent_runs))
    def forbidden(*args):pytest.fail('Read-only node attempted a write')
    monkeypatch.setattr(refs,'put_if_current',forbidden)
    for _ in range(2):assert price_validation(state)['price_check']==PriceCheck.PASS
    forged=target.model_copy(update={'target_unit_price':'1'})
    assert price_validation({**state,'counter_offer':forged})['price_check']==PriceCheck.FIX
    assert (refs.records,stored,common.agent_runs)==before


def test_repeated_comparison_and_sensitivity_do_not_store(team_storage):
    full_volume(team_storage);missing_shipping(team_storage)
    common,scope,facts,refs,repo=team_storage
    before=deepcopy((refs.records,common.agent_runs,common.quote_versions,facts.facts))
    for _ in range(2):
        assert tools.inspect_comparability(repo,scope)['status']=='NEEDS_EVIDENCE'
        assert tools.compute_break_even(repo,scope,'qv-b')['calculation_ref'] is None
    assert (refs.records,common.agent_runs,common.quote_versions,facts.facts)==before


def priority_runtime(storage):
    from types import SimpleNamespace
    common,scope,_,_,_=storage
    data=PriorityIn(case_id=CASE,input_revision=scope.revision,quote_version_ids=list(scope.quote_ids))
    run=common.start_agent_run(CASE,R.INFO_PRIORITY,scope.revision,data.model_dump(mode='json'),'SYNTHETIC','test')
    return SimpleNamespace(context=RunContext(CASE,scope.revision,run['agent_run_id']),
        config={'metadata':{'lc_agent_name':'info_priority'}})


def priority_input(storage):
    return PriorityIn(case_id=CASE,input_revision=storage[1].revision,quote_version_ids=list(storage[1].quote_ids))


@pytest.mark.parametrize('fault',['revision','source','not_saved','exception','missing_writer','missing_reader'])
def test_break_even_store_failure_never_issues_a_reference(team_storage,monkeypatch,fault):
    missing_shipping(team_storage)
    runtime=priority_runtime(team_storage)
    common,scope,facts,refs,_=team_storage
    store=tools._priority_store
    if fault=='revision':refs.before_write=lambda:common.work_cases[CASE].update(input_revision=2)
    if fault=='source':refs.before_write=lambda:common.work_cases[CASE].update(quantity='1001')
    if fault=='not_saved':monkeypatch.setattr(store,'save_break_even',lambda *args:None)
    if fault=='missing_writer':monkeypatch.setattr(store,'save_break_even',None)
    if fault=='missing_reader':monkeypatch.setattr(store,'read_break_even',None)
    if fault=='exception':
        def fail(*args):raise ConnectionError('SYNTHETIC write failure')
        monkeypatch.setattr(store,'save_break_even',fail)
    call=lambda:tools.calculate_break_even.func(list(scope.quote_ids),'qv-b',runtime)
    if fault in ('revision','source','exception'):
        with pytest.raises((OutputValidationError,ConnectionError)):call()
    else:
        result=call()
        assert result['status']=='HOLD' and not result.get('calculation_ref') and not result.get('question')
    assert not store.priority_records


@pytest.mark.parametrize('constraint',['capacity','moq'])
def test_quantity_warning_allows_conditional_comparison_but_not_allocation(team_storage,constraint):
    common,scope,facts,_,repo=team_storage
    if constraint=='moq':
        row=common.quote_versions['qv-b']
        row.pricing_terms_jsonb['moq']='1200'
        row.evidence_jsonb['moq'].update(raw_value='1200',value='1200')
        common.mail_messages['mail-qv-b']['body_text']=common.mail_messages['mail-qv-b']['body_text'].replace('moq: 0','moq: 1200')
        facts.refresh_binding()
    # With known costs the purchase calculator must still reject a full allocation.
    bundle=tools.compute_plan_set(repo.snapshot(scope)['data'])
    switch=[p for p in bundle['plans'] if p.get('proposal',p)['type']=='SWITCH']
    assert switch and all(p['status']=='INVALID' and p['issues'][0]['code']=='QUANTITY_NOT_FEASIBLE' for p in switch)
    missing_shipping(team_storage)
    result=tools.calculate_break_even.func(list(scope.quote_ids),'qv-b',priority_runtime(team_storage))
    assert result['status']=='STORED_CONDITIONAL' and result['result_kind']=='CONDITIONAL'
    assert result['allocation_feasible'] is False and result['warnings']
    assert {'SUPPLY_SHORTAGE' if constraint=='capacity' else 'BELOW_MOQ'} <= {w['code'] for w in result['warnings']}
    assert result['question']['calculation_ref']==result['calculation_ref']
    assert result['question']['break_even_amount']==result['break_even_amount']
    assert result['question']['priority']=='CAN_CHANGE_CHOICE'


@pytest.fixture
def priority_request(team_storage,monkeypatch):
    missing_shipping(team_storage)
    saved=install_clarification_storage_double(team_storage,monkeypatch)
    output=run_priority(team_storage)
    assert output.next_action=='REQUEST_CLARIFICATION'
    return team_storage,saved,output


def test_priority_final_guard_is_read_only_with_stored_calculation_and_draft(priority_request,monkeypatch):
    storage,saved,output=priority_request
    common,scope,_,refs,_=storage
    store=tools._priority_store
    before=deepcopy((store.priority_records,refs.records,saved,common.agent_runs))
    def forbidden(*args):pytest.fail('Final priority guard wrote')
    monkeypatch.setattr(store,'save_break_even',forbidden)
    monkeypatch.setattr(refs,'put_if_current',forbidden)
    monkeypatch.setattr(common,'finish_agent_run',forbidden)
    for _ in range(2):
        assert check_final_output(R.INFO_PRIORITY,priority_input(storage),RunContext(CASE,scope.revision),output)==output
    assert (store.priority_records,refs.records,saved,common.agent_runs)==before


@pytest.mark.parametrize('fault',['supplier','field','evidence','amount','reference','missing_calculation',
                                  'can_change_without_proof','low_without_proof','downgrade_with_proof',
                                  'duplicate','omitted_question','draft_id','draft_group','draft_revision','draft_send','no_lookup'])
def test_priority_guard_rejects_forged_questions_and_draft_groups(priority_request,monkeypatch,fault):
    storage,saved,original=priority_request
    output=original.model_copy(deep=True);question=output.questions[0]
    if fault=='supplier':question.supplier_id='foreign'
    if fault=='field':question.field='invented'
    if fault=='evidence':question.evidence_refs=['qv-a']
    if fault=='amount':question.break_even_amount='1'
    if fault=='reference':question.calculation_ref='invented'
    if fault=='missing_calculation':question.calculation_ref=question.break_even_amount=None
    if fault in ('can_change_without_proof','low_without_proof'):
        question.priority=QuestionPriority.CAN_CHANGE_CHOICE if fault.startswith('can') else QuestionPriority.LOW_VALUE
        question.calculation_ref=question.break_even_amount=None
    if fault=='downgrade_with_proof':question.priority=QuestionPriority.LOW_VALUE
    if fault=='duplicate':output.questions.append(question.model_copy())
    if fault=='omitted_question':output.questions=[]
    if fault=='draft_id':output.draft_ids=['nonexistent']
    if fault=='draft_group':saved[output.draft_ids[0]]['questions']=[]
    if fault=='draft_revision':saved[output.draft_ids[0]]['input_revision']=2
    if fault=='draft_send':saved[output.draft_ids[0]]['send_authorized']=True
    if fault=='no_lookup':monkeypatch.setattr(tools,'_draft_lookup_store',None)
    before=deepcopy((tools._priority_store.priority_records,storage[3].records,saved))
    with pytest.raises((OutputValidationError,KeyError)):
        check_final_output(R.INFO_PRIORITY,priority_input(storage),RunContext(CASE,1),output)
    assert (tools._priority_store.priority_records,storage[3].records,saved)==before


@pytest.mark.parametrize('fault',['missing','source_answer','policy'])
def test_priority_guard_rejects_build_plans_before_ready(team_storage,fault):
    if fault=='missing':absent_field(team_storage,'unit_price')
    if fault=='source_answer':absent_field(team_storage,'unit_price',remove_source=False)
    if fault=='policy':
        team_storage[0].work_cases[CASE]['policy']=None;team_storage[2].refresh_binding()
    with pytest.raises(OutputValidationError,match='PRIORITY_BUILD_NOT_READY'):
        check_final_output(R.INFO_PRIORITY,priority_input(team_storage),RunContext(CASE,1),
                           {'next_action':'BUILD_PLANS','reason':'forged'})


def test_priority_guard_accepts_ready_build_and_policy_hold(team_storage):
    result=run_priority(team_storage)
    assert check_final_output(R.INFO_PRIORITY,priority_input(team_storage),RunContext(CASE,1),result)==result
    team_storage[0].work_cases[CASE]['policy']=None;team_storage[2].refresh_binding()
    result=run_priority(team_storage)
    assert result.next_action=='HOLD'
    assert check_final_output(R.INFO_PRIORITY,priority_input(team_storage),RunContext(CASE,1),result)==result


@pytest.mark.parametrize('ids',[[],['qv-a'],['qv-a','qv-b','qv-b'],['qv-a','foreign']])
def test_typed_quote_tool_arguments_must_match_trusted_scope(team_storage,ids):
    runtime=priority_runtime(team_storage)
    with pytest.raises(OutputValidationError,match='TOOL_SCOPE_QUOTE_MISMATCH'):
        tools.check_comparability.func(ids,runtime)
    assert not tools._priority_store.priority_records


def test_initial_plan_graph_without_quote_set_reproduces_owner_gap(team_storage):
    from app.agents.graph import run_case
    from app.agents.schemas import RunCaseInput
    result=run_case(team_graph(team_storage),RunCaseInput(case_id=CASE,input_revision=1,start_from='PLAN'))
    assert result.status=='ON_HOLD' and 'QUOTE_VERSION_IDS_REQUIRED' in result.unresolved
    assert team_storage[0].agent_runs['run-1']['input_snapshot_jsonb']['quote_version_ids']==[]
    assert set(team_storage[0].get_work_case(CASE)['latest_quote_version_ids'])=={'qv-a','qv-b'}


def test_partial_reply_graph_loses_untouched_supplier_quote_reproduction(team_storage,monkeypatch):
    from app.agents.graph import run_case
    from app.agents.schemas import RunCaseInput
    model=extraction_model(team_storage,monkeypatch)
    model.extraction_args=model.extraction_args[1:]
    result=run_case(team_graph(team_storage,model),RunCaseInput(case_id=CASE,input_revision=1,
                   start_from='REPLY',mail_ids=['mail-qv-b']))
    assert result.status=='FAILED' and len(result.steps)==1 and result.steps[0].role==R.INFO_EXTRACTOR
    row=team_storage[0].agent_runs['run-2']
    incoming=row['input_snapshot_jsonb']['quote_version_ids']
    current=team_storage[0].get_work_case(CASE)['latest_quote_version_ids']
    assert len(incoming)==1 and set(incoming)<set(current) and row['status']=='FAILED'
    with pytest.raises(OutputValidationError,match='STALE_QUOTE_VERSIONS'):
        tools._ReviewAccess(RunContext(CASE,1),R.INFO_PRIORITY).check_scope(ExecutionScope(CASE,1,tuple(sorted(incoming))))


@pytest.mark.parametrize('fault',['revision','source','record','foreign_case'])
def test_priority_final_guard_rejects_stale_or_foreign_calculation(priority_request,fault):
    storage,_,output=priority_request
    common,scope,facts,_,_=storage
    context=RunContext(CASE,1)
    if fault=='revision':common.work_cases[CASE]['input_revision']=2
    if fault=='source':
        common.work_cases[CASE]['quantity']='900';facts.refresh_binding()
    if fault=='record':
        tools._priority_store.priority_records[output.questions[0].calculation_ref]['record']['break_even_amount']='1'
    if fault=='foreign_case':context=RunContext('foreign',1)
    with pytest.raises(OutputValidationError):
        check_final_output(R.INFO_PRIORITY,priority_input(storage),context,output)


def test_conditional_sensitivity_does_not_extrapolate_missing_tier_price(team_storage):
    from test_agents45_price_tiers import stored_tiers
    stored_tiers(team_storage, [(None,'600kg 이하','5400')], capacity='600')
    missing_shipping(team_storage)
    # The actual current quote table covers its feasible range only. A capacity
    # warning cannot manufacture a price outside the table's stated intervals.
    result=tools.compute_break_even(team_storage[4],team_storage[1],'qv-b')
    assert result['status']=='NOT_CALCULABLE' and result['missing_inputs']


def test_unknown_handling_can_change_choice_at_fractional_switching_boundary(team_storage):
    full_volume(team_storage)
    common,scope,facts,_,repo=team_storage
    row=common.quote_versions['qv-b']
    handling=next(c for c in row.cost_terms_jsonb if c['cost_type']=='HANDLING')
    row.cost_terms_jsonb=[c for c in row.cost_terms_jsonb if c['cost_type']!='HANDLING']
    mail=common.mail_messages['mail-qv-b']
    mail['body_text']=mail['body_text'].replace(handling['source_text'],'HANDLING: unknown')
    fact=facts.facts['qv-b.switching_cost']
    old=fact['evidence']['excerpt']
    fact['value']='90000.004'
    fact['evidence']['excerpt']='qv-b.switching_cost='+json.dumps(fact['value'])
    source=common.mail_messages['review-facts']
    source['body_text']=source['body_text'].replace(old,fact['evidence']['excerpt'])
    facts.refresh_binding()
    result=tools.compute_break_even(repo,scope,'qv-b')
    assert result['status']=='CALCULATED' and result['feasible_nonnegative_cost'] is True
    assert Decimal(result['raw_upper_exclusive'])==Decimal('0.001')
    assert tools.money(Decimal(result['offset']))<=Decimal(result['rounded_component_limit'])


def set_review_cost(storage, qid='qv-b', *, kind='SHIPPING', amount='10000', basis=None, included=False):
    common,_,facts,_,_=storage
    row=common.quote_versions[qid]
    cost=next(c for c in row.cost_terms_jsonb if c['cost_type']==kind)
    old=cost['source_text']
    text=f"{kind}: {amount+'원' if amount is not None else '미확인'} {basis or ''} 구매사"
    if included is not None:text+=' 상품가격에 포함' if included else ' 별도 부담'
    cost.update(source_text=text,amount=amount,basis=basis,payer='구매사',included_in_price=included)
    common.mail_messages[row.source_mail_id]['body_text']=common.mail_messages[row.source_mail_id]['body_text'].replace(old,text)
    if kind=='SHIPPING':row.shipping_cost=amount
    facts.refresh_binding()


def case_hundred(storage):
    common,_,facts,_,_=storage
    common.work_cases[CASE]['quantity']='100'
    row=common.quote_versions['qv-b'];row.unit_price='5600'
    row.evidence_jsonb['unit_price'].update(value='5600',raw_value='5600')
    common.mail_messages[row.source_mail_id]['body_text']=common.mail_messages[row.source_mail_id]['body_text'].replace('unit_price: 5400','unit_price: 5600')
    facts.refresh_binding()
    set_review_cost(storage,amount='0')


@pytest.mark.parametrize('scenario',['no_basis','per_order','per_unit','zero','cost_priority','incumbent_unknown'])
def test_external_spec_cost_and_priority_regressions(team_storage,scenario):
    _,scope,_,_,repo=team_storage
    if scenario in ('no_basis','per_order','per_unit','zero'):
        set_review_cost(team_storage,amount='10' if scenario=='per_unit' else '0' if scenario=='zero' else '10000',
            basis={'per_order':'회당','per_unit':'kg당'}.get(scenario),included=None if scenario=='zero' else False)
        data=repo.snapshot(scope)['data']
        if scenario=='per_unit':
            bundle=tools.compute_plan_set(data)
            split=next(p for p in bundle['plans'] if p['status']=='VALID' and p['type']=='SPLIT' and
                next(a['quantity'] for a in p['allocations'] if a['supplier_id']=='supplier-b')=='300')
            assert next(c for c in split['supplier_costs'] if c['supplier_id']=='supplier-b')['costs']['transport']=='3000'
        else:assert data['quotes'][1]['costs']['transport']==('0' if scenario=='zero' else '10000')
    elif scenario=='cost_priority':
        missing_shipping(team_storage)
        assert run_priority(team_storage).questions[0].priority=='CAN_CHANGE_CHOICE'
    else:
        case_hundred(team_storage);missing_shipping(team_storage,'qv-a')
        result=tools.compute_break_even(repo,scope,'qv-b')
        assert result['status']=='CALCULATED' and Decimal(result['break_even_amount'])==Decimal('10000')
        assert result['unknown_side']=='INCUMBENT' and result['comparison_operator']=='>='


@pytest.mark.parametrize('basis',[None,'회당','1회','건당','kg당','박스당'])
def test_order_and_quantity_cost_bases(team_storage,basis):
    set_review_cost(team_storage,amount='10',basis=basis)
    _,scope,_,_,repo=team_storage
    if basis=='박스당':
        report=tools.inspect_comparability(repo,scope)
        assert report['status']=='NEEDS_EVIDENCE'
        assert report['quotes'][1]['confirmed']['SHIPPING'] is None
    else:
        data=repo.snapshot(scope)['data']
        assert data['quotes'][1]['costs']['transport']==('10000' if basis=='kg당' else '10')


@pytest.mark.parametrize('fault',['amount','inclusion','source_amount','source_inclusion','source_unit_basis'])
def test_cost_unknown_and_unextracted_source_are_distinct(team_storage,fault):
    set_review_cost(team_storage,amount=None if fault=='amount' else '10',
        included=None if fault=='inclusion' else False,basis='kg당' if fault=='source_unit_basis' else None)
    common,scope,facts,_,repo=team_storage
    cost=common.quote_versions['qv-b'].cost_terms_jsonb[0]
    if fault=='source_amount':
        cost['amount']=None;common.quote_versions['qv-b'].shipping_cost=None
    if fault=='source_inclusion':cost['included_in_price']=None
    if fault=='source_unit_basis':cost['basis']=None
    facts.refresh_binding()
    report=tools.inspect_comparability(repo,scope)
    assert report['status']==('RECHECK_SOURCE' if fault.startswith('source_') else 'NEEDS_EVIDENCE')
    assert report['quotes'][1]['confirmed']['SHIPPING'] is None
    if fault.startswith('source_'):assert not report['questions']


def test_quantity_cost_recalculated_in_plan_and_offer_and_independently_checked(team_storage,monkeypatch):
    from test_agents45_price_tiers import run_counter
    set_review_cost(team_storage,amount='10',basis='kg당')
    plan=run_plan(team_storage)
    assert plan.next_action=='PLANS_PROPOSED'
    run_counter(team_storage,plan)
    refs=team_storage[3].records.values()
    bundle=next(r['payload']['bundle'] for r in refs if r['kind']=='PLAN_SET')
    offer=next(r['payload']['result'] for r in refs if r['kind']=='OFFER_CALCULATION')
    for rows in (bundle['plans'],offer['comparison']['plans_at_proposed_price']):
        seen=set()
        for row in rows:
            if row['status']!='VALID':continue
            quantity=sum(Decimal(a['quantity']) for a in row['allocations'] if a['supplier_id']=='supplier-b')
            if not quantity:continue
            cost=next(c for c in row['supplier_costs'] if c['supplier_id']=='supplier-b')
            assert Decimal(cost['costs']['transport'])==quantity*10
            seen.add(quantity)
        assert {Decimal(300),Decimal(600)}<=seen
    original=tools.allocation_costs
    def wrong(quote,quantity):
        result=original(quote,quantity)
        if quote.get('cost_rates'):result['transport']='1'
        return result
    monkeypatch.setattr(tools,'allocation_costs',wrong)
    result=run_plan(team_storage)
    assert result.next_action=='HOLD' and result.unknown_fields==['INDEPENDENT_PLAN_COST_MISMATCH']


@pytest.mark.parametrize('side',['INCUMBENT','ALTERNATIVE','BOTH'])
def test_unknown_either_side_stored_proof_and_read_only_final_guard(team_storage,monkeypatch,side):
    case_hundred(team_storage)
    if side in ('INCUMBENT','BOTH'):missing_shipping(team_storage,'qv-a')
    if side in ('ALTERNATIVE','BOTH'):missing_shipping(team_storage,'qv-b')
    saved=install_clarification_storage_double(team_storage,monkeypatch)
    result=run_priority(team_storage)
    assert result.next_action==('HOLD' if side=='ALTERNATIVE' else 'REQUEST_CLARIFICATION')
    records=tools._priority_store.priority_records
    before=deepcopy((records,saved,team_storage[3].records))
    for question in result.questions:
        assert question.calculation_ref
        record=tools.read_break_even(team_storage[4],team_storage[1],question.calculation_ref)
        assert record.unknown_side==side
        if side=='BOTH':
            assert record.result_kind=='RELATION' and question.break_even_amount is None
            assert record.baseline_total is None
            assert 'A_SHIPPING' in record.condition and 'B_SHIPPING' in record.condition
            assert '550000' in record.condition and '560000' in record.condition
        else:
            assert record.comparison_operator==('>=' if side=='INCUMBENT' else '<=')
            assert Decimal(question.break_even_amount)==(Decimal(10000) if side=='INCUMBENT' else Decimal(-10000))
    monkeypatch.setattr(tools._priority_store,'save_break_even',lambda *args:pytest.fail('Final guard wrote'))
    for _ in range(2):
        assert check_final_output(R.INFO_PRIORITY,priority_input(team_storage),RunContext(CASE,1),result)==result
    assert (records,saved,team_storage[3].records)==before


def test_cost_low_value_requires_dominance_and_cannot_be_manually_reclassified(team_storage,monkeypatch):
    common,scope,facts,_,repo=team_storage
    row=common.quote_versions['qv-b'];row.unit_price='6000'
    row.evidence_jsonb['unit_price'].update(value='6000',raw_value='6000')
    common.mail_messages[row.source_mail_id]['body_text']=common.mail_messages[row.source_mail_id]['body_text'].replace('unit_price: 5400','unit_price: 6000')
    facts.refresh_binding();missing_shipping(team_storage)
    install_clarification_storage_double(team_storage,monkeypatch)
    result=run_priority(team_storage)
    assert result.questions[0].priority=='LOW_VALUE'
    record=tools.read_break_even(repo,scope,result.questions[0].calculation_ref)
    assert 'STRICTLY_DOMINATED' in record.priority_basis
    assert check_final_output(R.INFO_PRIORITY,priority_input(team_storage),RunContext(CASE,1),result)==result
    result.questions[0].priority=QuestionPriority.MUST_CONFIRM
    with pytest.raises(OutputValidationError,match='CLASSIFICATION_MISMATCH'):
        check_final_output(R.INFO_PRIORITY,priority_input(team_storage),RunContext(CASE,1),result)


def test_not_calculable_is_never_low_value(team_storage):
    missing_shipping(team_storage)
    set_review_cost(team_storage,kind='HANDLING',amount=None)
    report=tools.inspect_comparability(team_storage[4],team_storage[1])
    assert tools.compute_break_even(team_storage[4],team_storage[1],'qv-b')['status']=='NOT_CALCULABLE'
    assert all(q['priority']=='MUST_CONFIRM' and q['calculation_ref'] is None for q in report['questions'])


def test_incumbent_unknown_lower_boundary_is_inclusive(team_storage):
    case_hundred(team_storage);missing_shipping(team_storage,'qv-a')
    result=tools.compute_break_even(team_storage[4],team_storage[1],'qv-b')
    assert Decimal(result['raw_lower_inclusive'])==Decimal('9999.995')
    assert result['raw_upper_exclusive'] is None and result['baseline_total'] is None
    for amount,changes in [('9999.9949',False),('9999.995',True),('10000',True)]:
        assert (tools.money(Decimal(amount))>=Decimal(result['rounded_component_limit'])) is changes


def low_value_shipping(storage):
    common,_,facts,_,_=storage
    row=common.quote_versions['qv-b'];previous=row.unit_price;row.unit_price='6000'
    row.evidence_jsonb['unit_price'].update(value='6000',raw_value='6000')
    common.mail_messages[row.source_mail_id]['body_text']=common.mail_messages[row.source_mail_id]['body_text'].replace(
        'unit_price: '+previous,'unit_price: 6000')
    facts.refresh_binding();missing_shipping(storage)


@pytest.mark.parametrize('scenario',['included_without_amount','low_value_no_inquiry'])
def test_external_followup_included_and_low_value(team_storage,monkeypatch,scenario):
    if scenario=='included_without_amount':
        set_review_cost(team_storage,amount=None,included=True)
        assert tools._inclusion(team_storage[0].quote_versions['qv-b'].cost_terms_jsonb[0]['source_text']) is True
        report=tools.inspect_comparability(team_storage[4],team_storage[1])
        assert report['status']=='PASS',report['blocking_issues']
        assert report['quotes'][1]['confirmed']['SHIPPING']=='0'
    else:
        low_value_shipping(team_storage)
        saved=install_clarification_storage_double(team_storage,monkeypatch)
        result=run_priority(team_storage)
        assert result.next_action=='HOLD'
        assert not result.draft_ids and not saved
        assert result.questions[0].priority=='LOW_VALUE' and result.questions[0].calculation_ref
        assert result.unknown_fields==['LOW_VALUE_UNCONFIRMED_COST_PLAN_CONTRACT_REQUIRED']


@pytest.mark.parametrize('kind',['SHIPPING','HANDLING'])
def test_included_cost_without_amount_reaches_plan_and_offer_without_imputation(team_storage,kind):
    from test_agents45_price_tiers import run_counter
    set_review_cost(team_storage,kind=kind,amount=None,included=True)
    common,scope,_,_,repo=team_storage
    data=repo.snapshot(scope)['data']
    key='transport' if kind=='SHIPPING' else 'other_logistics'
    assert data['quotes'][1]['costs'][key]=='0'
    assert next(c for c in common.quote_versions['qv-b'].cost_terms_jsonb if c['cost_type']==kind)['amount'] is None
    result=run_plan(team_storage)
    assert result.next_action=='PLANS_PROPOSED'
    assert run_counter(team_storage,result).unknown_fields==['create_mail_draft']


@pytest.mark.parametrize('fault',['missing_inclusion','false_inclusion','forged_true','changed_source','forged_amount'])
def test_included_cost_zero_requires_original_evidence(team_storage,fault):
    set_review_cost(team_storage,amount=None,included=None if fault=='missing_inclusion' else fault!='false_inclusion')
    common,scope,facts,_,repo=team_storage
    row=common.quote_versions['qv-b'];cost=row.cost_terms_jsonb[0]
    if fault=='forged_true':
        old=cost['source_text'];cost['source_text']=old.replace('상품가격에 포함','별도 부담')
        common.mail_messages[row.source_mail_id]['body_text']=common.mail_messages[row.source_mail_id]['body_text'].replace(old,cost['source_text'])
    if fault=='changed_source':
        common.mail_messages[row.source_mail_id]['body_text']=common.mail_messages[row.source_mail_id]['body_text'].replace(cost['source_text'],'SHIPPING: 별도 부담')
    if fault=='forged_amount':cost['amount']='99999'
    facts.refresh_binding()
    report=tools.inspect_comparability(repo,scope)
    assert report['status']==('NEEDS_EVIDENCE' if fault in ('missing_inclusion','false_inclusion') else 'RECHECK_SOURCE')
    assert report['quotes'][1]['confirmed']['SHIPPING'] is None


def test_only_low_value_stops_real_graph_without_draft_or_reply_wait(team_storage,monkeypatch):
    low_value_shipping(team_storage)
    saved=install_clarification_storage_double(team_storage,monkeypatch)
    state=team_graph(team_storage).invoke(graph_state(team_storage))
    assert state['status']=='ON_HOLD' and not state['draft_ids'] and not saved
    # graph.py accumulates the earlier ④ missing-cost record and HOLD reason.
    assert state['unresolved'][:2]==['cost_terms',tools.LOW_VALUE_PLAN_CONTRACT_REQUIRED]
    assert state['steps'][-1].unknown_fields==[tools.LOW_VALUE_PLAN_CONTRACT_REQUIRED]
    assert state['steps'][-1].next_action=='HOLD'
    assert all(s.next_action!='REQUEST_CLARIFICATION' for s in state['steps'])
    assert [s.role for s in state['steps']]==[R.RESPONSE_PLAN,R.INFO_PRIORITY]
    assert tools._priority_store.priority_records
    report=tools.inspect_comparability(team_storage[4],team_storage[1])
    assert len(report['quotes'])==2 and report['quotes'][1]['confirmed']['SHIPPING'] is None
    assert not report['question_groups'] and report['questions'][0]['priority']=='LOW_VALUE'


def test_low_value_hold_has_no_draft_dependency_and_final_guard_is_read_only(team_storage,monkeypatch):
    low_value_shipping(team_storage)
    monkeypatch.setattr(tools,'_draft_lookup_store',None)
    report=tools.check_comparability.func(list(team_storage[1].quote_ids),priority_runtime(team_storage))
    assert report['next_action']=='HOLD' and report['draft_required'] is False
    assert report['missing_dependencies']==[tools.LOW_VALUE_PLAN_CONTRACT_REQUIRED]
    result=run_priority(team_storage)
    before=deepcopy((tools._priority_store.priority_records,team_storage[3].records))
    monkeypatch.setattr(tools._priority_store,'save_break_even',lambda *args:pytest.fail('Final guard wrote'))
    for _ in range(2):
        assert check_final_output(R.INFO_PRIORITY,priority_input(team_storage),RunContext(CASE,1),result)==result
    assert (tools._priority_store.priority_records,team_storage[3].records)==before
    for change in ({'draft_ids':['unnecessary']},{'next_action':'REQUEST_CLARIFICATION'},
                   {'next_action':'BUILD_PLANS'},{'questions':[]},{'unknown_fields':['cost_terms']}):
        with pytest.raises(OutputValidationError):
            check_final_output(R.INFO_PRIORITY,priority_input(team_storage),RunContext(CASE,1),result.model_copy(update=change))


def test_inquiry_groups_preserve_assessments_but_only_select_actionable_priorities():
    questions=[Question(supplier_id='supplier-b',field=field,priority=priority,reason='explicit assessment')
               .model_dump(mode='json') for field,priority in [('required','MUST_CONFIRM'),('cost','CAN_CHANGE_CHOICE'),('low','LOW_VALUE')]]
    original=deepcopy(questions)
    groups=tools.inquiry_question_groups(questions)
    assert [q['field'] for q in groups[0]['questions']]==['required','cost']
    assert questions==original


def test_final_guard_mixed_draft_subset_contract(team_storage,monkeypatch):
    """Guard unit test: explicit comparison-result double for mixed assessments.

    Uses a real stored LOW_VALUE proof; does not claim mixed-source integration.
    """
    low_value_shipping(team_storage)
    saved=install_clarification_storage_double(team_storage,monkeypatch)
    result=run_priority(team_storage)
    report=tools.inspect_comparability(team_storage[4],team_storage[1])
    required=Question(supplier_id='supplier-b',field='available_quantity',priority='MUST_CONFIRM',
                      reason='SYNTHETIC comparison-result double',evidence_refs=['qv-b'])
    report['questions'].append(required.model_dump(mode='json'))
    report['question_groups']=tools.inquiry_question_groups(report['questions'])
    monkeypatch.setattr(tools,'inspect_comparability',lambda *args:deepcopy(report))
    result.questions.append(required)
    result.next_action=type(result.next_action).REQUEST_CLARIFICATION;result.unknown_fields=report['unknown_fields']
    draft=tools._registry['create_mail_draft'].func('CLARIFICATION','supplier-b',[required],priority_runtime(team_storage))
    result.draft_ids=[draft['draft_id']]
    assert check_final_output(R.INFO_PRIORITY,priority_input(team_storage),RunContext(CASE,1),result)==result
    saved[draft['draft_id']]['questions'].append(result.questions[0].model_dump(mode='json'))
    with pytest.raises(OutputValidationError,match='CLARIFICATION_DRAFT_MISMATCH'):
        check_final_output(R.INFO_PRIORITY,priority_input(team_storage),RunContext(CASE,1),result)


@pytest.mark.parametrize('phrase',['배송비 상품가격에 포함','배송비 단가 포함'])
def test_included_cost_wording_through_registered_plan_and_offer(team_storage,phrase):
    from test_agents45_price_tiers import run_counter
    set_review_cost(team_storage,amount=None,included=True)
    common,_,facts,refs,_=team_storage
    row=common.quote_versions['qv-b'];cost=row.cost_terms_jsonb[0];old=cost['source_text']
    cost.update(source_text=phrase,payer=None)
    common.mail_messages[row.source_mail_id]['body_text']=common.mail_messages[row.source_mail_id]['body_text'].replace(old,phrase)
    facts.refresh_binding()
    plan=run_plan(team_storage)
    assert plan.next_action=='PLANS_PROPOSED',plan
    assert {a.quote_version_id for p in plan.plans for a in p.allocations}=={'qv-a','qv-b'}
    result=run_counter(team_storage,plan)
    assert result.next_action=='HOLD' and result.unknown_fields==['create_mail_draft']
    bundle=next(r['payload']['bundle'] for r in refs.records.values() if r['kind']=='PLAN_SET')
    for p in bundle['plans']:
        if p['status']=='VALID':
            for c in p['supplier_costs']:
                if c['supplier_id']=='supplier-b':assert c['costs']['transport']=='0'
    assert cost['amount'] is None


@pytest.mark.parametrize('source,valid',[
    ('단가: 5,500원/kg\n배송비 단가 포함',True),
    ('단가: 5500원/kg; 배송비 단가에 포함',True),
    ('단가: 5500원/kg\n배송비 단가 포함, 배송비 900원',True),
    ('배송비 단가 포함',False),
    ('배송비 단가 포함 5500원',False),
    ('수량: 5500\n배송비 단가 포함',False),
    ('단가: 15500원/kg\n배송비 단가 포함 5500원',False),
    ('단가: 6500원/kg\n배송비 단가 포함',False),
    ('단가: 15500원/kg\n참고값: 5500\n배송비 단가 포함',False),
    ('단가: 5500원/kg\n변경 단가: 6500원/kg\n배송비 단가 포함',False),
    ('단가: -5500원/kg\n배송비 단가 포함',False),
    ('단가: 55\n00\n배송비 단가 포함',False),
])
def test_numeric_source_cost_inclusion_is_not_a_price_or_number_donor(source,valid):
    assert tools.team_bridge_team_data_numeric_field_source(source,'5500','unit_price') is valid
