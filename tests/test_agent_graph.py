from dataclasses import replace
from uuid import uuid4

import pytest

from app.agents.graph import build_graph, run_case
from app.agents.nodes import Nodes
from app.agents.schemas import (AGENT_IO, AgentRole, PlanCheck, PriceCheck, ProcessingStep, RunCaseInput,
                                RunStatus)

R = AgentRole


class ScriptedAgents:
    """역할마다 next_action 목록을 순서대로 돌려주는 가짜 에이전트."""

    def __init__(self, **scripts):
        self.scripts = {R[role]: list(actions) for role, actions in scripts.items()}
        self.calls, self.inputs = [], []

    def __call__(self, role, data):
        self.calls.append(role)
        self.inputs.append(data)
        action = self.scripts[role].pop(0)
        output = AGENT_IO[role][1]
        drafting = action in ('SEND_RFQ', 'REQUEST_CLARIFICATION', 'COUNTER_OFFER_PROPOSED')
        extra = {'draft_ids': [f'{role}-draft']} if drafting else {}
        return output(next_action=action, reason='근거', unknown_fields=[f'{role}-unknown'], **extra)


def nodes(plan=(PlanCheck.PASS,), price=(PriceCheck.PASS,)):
    plan, price = list(plan), list(price)
    return Nodes(preprocess=lambda s: {},
                 plan_validation=lambda s: {'plan_check': plan.pop(0)},
                 price_validation=lambda s: {'price_check': price.pop(0)},
                 review_card=lambda s: {'card_id': 'card-1'})


def run(agents, start_from='REPLY', node_set=None, revision=lambda case_id: 3):
    # Legacy full-graph regression remains explicit. Production defaults to ① only.
    graph = build_graph(agents, revision, node_set or nodes(), stop_after_extraction=False)
    return run_case(graph, RunCaseInput(case_id=uuid4(), start_from=start_from, input_revision=3, mail_ids=['m1']))


def test_first_run_extracts_selects_candidates_and_stops_for_sending():
    agents = ScriptedAgents(INFO_EXTRACTOR=['STORED'], CANDIDATE=['SEND_RFQ'])
    out = run(agents, 'START')
    assert agents.calls == [R.INFO_EXTRACTOR, R.CANDIDATE]
    assert out.status == RunStatus.WAITING_REPLY and out.draft_ids == ['CANDIDATE-draft']
    assert out.processing_step == ProcessingStep.SELECTING_CANDIDATES


def test_reply_runs_to_review_card():
    agents = ScriptedAgents(INFO_EXTRACTOR=['STORED'], INFO_PRIORITY=['BUILD_PLANS'], RESPONSE_PLAN=['PLANS_PROPOSED'],
                            COUNTER_OFFER=['COUNTER_OFFER_PROPOSED'])
    out = run(agents)
    assert agents.calls == [R.INFO_EXTRACTOR, R.INFO_PRIORITY, R.RESPONSE_PLAN, R.COUNTER_OFFER]
    assert out.status == RunStatus.COMPLETE and out.card_id == 'card-1'
    assert out.processing_step == ProcessingStep.REVIEW_READY


def test_missing_info_stops_with_clarification_draft():
    out = run(ScriptedAgents(INFO_EXTRACTOR=['STORED'], INFO_PRIORITY=['REQUEST_CLARIFICATION']))
    assert out.status == RunStatus.WAITING_REPLY and out.draft_ids == ['INFO_PRIORITY-draft']


@pytest.mark.parametrize('check,revisited', [(PlanCheck.FIX, [R.RESPONSE_PLAN]),
                                             (PlanCheck.NEED_INFO, [R.INFO_PRIORITY, R.RESPONSE_PLAN])])
def test_plan_validation_loops_back(check, revisited):
    agents = ScriptedAgents(INFO_EXTRACTOR=['STORED'], INFO_PRIORITY=['BUILD_PLANS', 'BUILD_PLANS'],
                            RESPONSE_PLAN=['PLANS_PROPOSED', 'PLANS_PROPOSED'], COUNTER_OFFER=['COUNTER_OFFER_PROPOSED'])
    out = run(agents, node_set=nodes(plan=[check, PlanCheck.PASS]))
    assert agents.calls == [R.INFO_EXTRACTOR, R.INFO_PRIORITY, R.RESPONSE_PLAN, *revisited, R.COUNTER_OFFER]
    assert out.status == RunStatus.COMPLETE


def test_price_validation_need_info_returns_to_priority():
    agents = ScriptedAgents(INFO_PRIORITY=['REQUEST_CLARIFICATION'], RESPONSE_PLAN=['PLANS_PROPOSED'],
                            COUNTER_OFFER=['COUNTER_OFFER_PROPOSED'])
    out = run(agents, 'PLAN', node_set=nodes(price=[PriceCheck.NEED_INFO]))
    assert agents.calls == [R.RESPONSE_PLAN, R.COUNTER_OFFER, R.INFO_PRIORITY]
    assert out.status == RunStatus.WAITING_REPLY


@pytest.mark.parametrize('check,status', [(PriceCheck.PASS, RunStatus.COMPLETE),
                                          (PriceCheck.NEED_INFO, RunStatus.WAITING_REPLY)])
def test_skipped_counter_offer_is_checked_by_price_validation(check, status):
    seen = []

    def price_validation(state):
        seen.append(state)
        return {'price_check': check}

    agents = ScriptedAgents(INFO_PRIORITY=['REQUEST_CLARIFICATION'], RESPONSE_PLAN=['PLANS_PROPOSED'],
                            COUNTER_OFFER=['COUNTER_OFFER_SKIPPED'])
    out = run(agents, 'PLAN', node_set=replace(nodes(), price_validation=price_validation))
    assert seen[0]['counter_offer'] is None and seen[0]['counter_offer_skip_reason'] == '근거'
    assert out.status == status
    if check == PriceCheck.PASS:
        assert out.processing_step == ProcessingStep.REVIEW_READY and out.draft_ids == []


def test_price_fix_returns_to_counter_offer_with_reason():
    agents = ScriptedAgents(RESPONSE_PLAN=['PLANS_PROPOSED'],
                            COUNTER_OFFER=['COUNTER_OFFER_PROPOSED', 'COUNTER_OFFER_PROPOSED'])
    checks = [{'price_check': PriceCheck.FIX, 'price_check_reason': 'TARGET_ABOVE_POLICY'},
              {'price_check': PriceCheck.PASS}]
    out = run(agents, 'PLAN', node_set=replace(nodes(), price_validation=lambda s: checks.pop(0)))
    assert agents.calls == [R.RESPONSE_PLAN, R.COUNTER_OFFER, R.COUNTER_OFFER]
    assert agents.inputs[1].price_check_reason is None
    assert agents.inputs[2].price_check_reason == 'TARGET_ABOVE_POLICY'
    assert out.status == RunStatus.COMPLETE


def test_steps_record_each_role_call_and_hold_reason():
    out = run(ScriptedAgents(INFO_EXTRACTOR=['STORED'], INFO_PRIORITY=['HOLD']))
    assert [(step.role, step.next_action, step.reason) for step in out.steps] == [
        (R.INFO_EXTRACTOR, 'STORED', '근거'), (R.INFO_PRIORITY, 'HOLD', '근거')]
    assert out.status == RunStatus.ON_HOLD and 'INFO_PRIORITY HOLD: 근거' in out.unresolved


def test_revision_change_before_review_card_marks_run_stale():
    calls = []

    def revision(case_id):
        calls.append(case_id)
        return 3 if len(calls) <= 4 else 4   # ④⑤ 전후에는 같고, 카드 직전에 바뀜

    cards = []
    agents = ScriptedAgents(RESPONSE_PLAN=['PLANS_PROPOSED'], COUNTER_OFFER=['COUNTER_OFFER_PROPOSED'])
    out = run(agents, 'PLAN', node_set=replace(nodes(), review_card=lambda s: cards.append(s) or {'card_id': 'c'}),
              revision=revision)
    assert out.status == RunStatus.STALE and out.card_id is None and cards == []


def test_counter_offer_needs_evidence_returns_to_priority():
    agents = ScriptedAgents(INFO_PRIORITY=['REQUEST_CLARIFICATION'], RESPONSE_PLAN=['PLANS_PROPOSED'],
                            COUNTER_OFFER=['NEEDS_EVIDENCE'])
    out = run(agents, 'PLAN')
    assert agents.calls == [R.RESPONSE_PLAN, R.COUNTER_OFFER, R.INFO_PRIORITY]
    assert out.status == RunStatus.WAITING_REPLY


def test_recheck_source_returns_to_extraction():
    agents = ScriptedAgents(INFO_EXTRACTOR=['STORED', 'STORED'], INFO_PRIORITY=['RECHECK_SOURCE', 'HOLD'])
    out = run(agents)
    assert agents.calls == [R.INFO_EXTRACTOR, R.INFO_PRIORITY, R.INFO_EXTRACTOR, R.INFO_PRIORITY]
    assert out.status == RunStatus.ON_HOLD and out.processing_step == ProcessingStep.ON_HOLD


def test_recheck_fields_reach_extractor_and_are_cleared():
    agents = ScriptedAgents(INFO_EXTRACTOR=['STORED', 'STORED'], INFO_PRIORITY=['RECHECK_SOURCE', 'HOLD'])
    run(agents)
    first, recheck, _ = (agents.inputs[0], agents.inputs[2], agents.inputs[3])
    assert first.recheck_fields == [] and recheck.recheck_fields == ['INFO_PRIORITY-unknown']


def test_counter_offer_receives_quotes_and_validated_plans():
    agents = ScriptedAgents(INFO_EXTRACTOR=['STORED'], INFO_PRIORITY=['BUILD_PLANS'], RESPONSE_PLAN=['PLANS_PROPOSED'],
                            COUNTER_OFFER=['COUNTER_OFFER_PROPOSED'])
    run(agents)
    counter_in = agents.inputs[-1]
    assert isinstance(counter_in, AGENT_IO[R.COUNTER_OFFER][0]) and counter_in.quote_version_ids == []


def test_role_call_limit_fails_instead_of_looping():
    agents = ScriptedAgents(INFO_EXTRACTOR=['STORED'] * 5, INFO_PRIORITY=['RECHECK_SOURCE'] * 5)
    out = run(agents)
    assert len(agents.calls) == 6
    assert out.status == RunStatus.FAILED and 'ROLE_CALL_LIMIT' in out.unresolved


def test_revision_change_marks_run_stale():
    agents = ScriptedAgents(INFO_EXTRACTOR=['STORED'], CANDIDATE=['SEND_RFQ'])
    out = run(agents, 'START', revision=lambda case_id: 4)
    assert agents.calls == []  # stale input is now rejected before the model call
    assert out.status == RunStatus.STALE and out.draft_ids == []


@pytest.mark.parametrize('failure', ['raise', 'wrong_type'])
def test_agent_error_or_wrong_output_fails(failure):
    def agent(role, data):
        if failure == 'raise':
            raise RuntimeError('model down')
        return AGENT_IO[R.CANDIDATE][1](next_action='SEND_RFQ', reason='근거')
    out = run(agent)
    assert out.status == RunStatus.FAILED and out.processing_step == ProcessingStep.FAILED


def test_node_error_fails():
    agents = ScriptedAgents(INFO_EXTRACTOR=['STORED'])
    broken = Nodes(preprocess=lambda s: (_ for _ in ()).throw(RuntimeError('ocr')))
    out = run(agents, node_set=broken)
    assert agents.calls == [] and out.status == RunStatus.FAILED and out.unresolved == ['preprocess: NODE_FAILED']
