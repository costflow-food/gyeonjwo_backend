"""③↔① 재추출 반복 차단 (2026-11-16 블루베리 업무 4f771fe2: GRAPH_TIME_LIMIT).

세금 문구("추가 세금·포장비 등 별도 가산 없음")는 견적에 이미 저장됐지만 tax_basis를 정하지 못해
check_comparability가 tax_terms를 missing_required로 냈다. ③은 같은 구절로 RECHECK_SOURCE를 반복했고
①은 매번 같은 결과를 내 300초 한도를 넘겼다.
"""
import json
import unittest
from uuid import uuid4

from langchain_core.messages import ToolMessage

from app.agents.agents import _priority_final_rules
from app.agents.graph import RECHECK_REPEATED_NOTE, build_graph, run_case
from app.agents.nodes import Nodes
from app.agents.schemas import (AgentNextAction as N, AgentRole, ExtractionOut, PlanOut, PriorityOut,
                                RunCaseInput, RunStatus, StartFrom)

TAX_TEXT = '추가 세금·포장비 등 별도 가산 없음'


def comparison(tax_terms_text):
    return ToolMessage(name='check_comparability', tool_call_id='c1', content=json.dumps({
        'status': 'OK', 'suppliers': [{'supplier_id': 'S1', 'missing_required': ['tax_terms'], 'unknown_costs': [],
                                       'requirements': [], 'tax_basis': None, 'tax_terms_text': tax_terms_text}]}))


def recheck(field='tax_terms', source=TAX_TEXT, supplier='S1'):
    return PriorityOut(next_action=N.RECHECK_SOURCE, reason='재추출', unknown_fields=[field],
                       recheck=[{'supplier_id': supplier, 'field': field, 'mail_id': 'M1', 'source_text': source}])


class PriorityRecheckRuleTests(unittest.TestCase):
    def test_stored_tax_wording_is_not_rechecked(self):
        problems = _priority_final_rules(recheck(), [comparison(TAX_TEXT)])
        self.assertEqual(len(problems), 1)
        self.assertIn('S1.tax_terms', problems[0])

    def test_unread_tax_terms_may_be_rechecked(self):
        self.assertEqual(_priority_final_rules(recheck(), [comparison(None)]), [])

    def test_other_fields_may_be_rechecked(self):
        answer = recheck(field='cost:SHIPPING', source='- 배송비: 당사 부담')
        self.assertEqual(_priority_final_rules(answer, [comparison(TAX_TEXT)]), [])


class Script:
    """역할별 가짜 응답을 차례로 돌려준다. 호출 순서를 기록한다."""
    def __init__(self, answers):
        self.answers = {role: list(items) for role, items in answers.items()}
        self.calls = []

    def __call__(self, role, data):
        self.calls.append(role)
        return self.answers[role].pop(0)


STORED = ExtractionOut(next_action=N.STORED, reason='저장', quote_version_ids=['Q1'])
PLAN_HOLD = PlanOut(next_action=N.HOLD, reason='보류')
NODES = Nodes(preprocess=lambda s: {})


def run(script):
    graph = build_graph(script, current_revision=lambda _: 1, nodes=NODES)
    return run_case(graph, RunCaseInput(case_id=uuid4(), start_from=StartFrom.REPLY, input_revision=1,
                                        mail_ids=['M1']))


class RecheckLoopGraphTests(unittest.TestCase):
    def test_same_recheck_twice_goes_on_to_plans(self):
        script = Script({AgentRole.INFO_EXTRACTOR: [STORED, STORED],
                         AgentRole.INFO_PRIORITY: [recheck(), recheck()],
                         AgentRole.RESPONSE_PLAN: [PLAN_HOLD]})
        result = run(script)
        I, P, R = AgentRole.INFO_EXTRACTOR, AgentRole.INFO_PRIORITY, AgentRole.RESPONSE_PLAN
        self.assertEqual(script.calls, [I, P, I, P, R])
        self.assertIn(RECHECK_REPEATED_NOTE, result.unresolved)
        self.assertEqual(result.status, RunStatus.ON_HOLD)

    def test_new_recheck_still_goes_back_to_extraction(self):
        script = Script({AgentRole.INFO_EXTRACTOR: [STORED, STORED, STORED],
                         AgentRole.INFO_PRIORITY: [recheck(), recheck(field='cost:SHIPPING', source='배송비: 당사 부담'),
                                                   PriorityOut(next_action=N.BUILD_PLANS, reason='진행')],
                         AgentRole.RESPONSE_PLAN: [PLAN_HOLD]})
        result = run(script)
        I, P, R = AgentRole.INFO_EXTRACTOR, AgentRole.INFO_PRIORITY, AgentRole.RESPONSE_PLAN
        # 다른 항목의 재확인은 ①로 돌아간다. 7회째 ④가 보류로 끝난다(역할 호출 한도 8 안)
        self.assertEqual(script.calls, [I, P, I, P, I, P, R])
        self.assertEqual(result.status, RunStatus.ON_HOLD)

    def test_plan_repeating_a_recheck_stops_with_hold(self):
        plan_recheck = PlanOut(next_action=N.RECHECK_SOURCE, reason='재추출', unknown_fields=['tax_terms'],
                               recheck=[{'supplier_id': 'S1', 'field': 'tax_terms', 'mail_id': 'M1',
                                         'source_text': TAX_TEXT}])
        script = Script({AgentRole.INFO_EXTRACTOR: [STORED, STORED],
                         AgentRole.INFO_PRIORITY: [recheck(), PriorityOut(next_action=N.BUILD_PLANS, reason='진행')],
                         AgentRole.RESPONSE_PLAN: [plan_recheck]})
        result = run(script)
        self.assertEqual(script.calls[-1], AgentRole.RESPONSE_PLAN)
        self.assertEqual(result.status, RunStatus.ON_HOLD)


if __name__ == '__main__':
    unittest.main()
