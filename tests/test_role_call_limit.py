"""KAN-326: 역할 호출 한도와 역할 후처리 실패 원인 기록.

11-18 망고 업무 f40a00e6: 회신 검토 ①③④⑤에서 ⑤가 인상 전 단가를 원문 재확인(RECHECK_SOURCE)하자 ①③ 뒤 ④가
7번째 호출이 되어 한도 6에 걸렸다(GRAPH_FAILED). 다시 시도 1회차에서는 ⑤가 끝난 뒤 실패했지만
agent_runs.error_jsonb에는 원인 없이 GRAPH_FAILED만 남았다(후처리가 실행기의 실패 기록 범위 밖이었다).
"""
import unittest
from unittest import mock
from uuid import uuid4

from app.agents import agents as agents_module
from app.agents.agents import AgentRunner
from app.agents.graph import build_graph, run_case
from app.agents.nodes import Nodes
from app.agents.schemas import (MAX_ROLE_CALLS_PER_RUN, AgentNextAction as N, AgentRole, CounterOfferOut,
                                ExtractionOut, PlanCheck, PlanIn, PlanOut, PriceCheck, PriorityOut, RunCaseInput,
                                RunStatus, StartFrom)
from app.core.errors import ServiceError

I, P, R, C = AgentRole.INFO_EXTRACTOR, AgentRole.INFO_PRIORITY, AgentRole.RESPONSE_PLAN, AgentRole.COUNTER_OFFER
STORED = ExtractionOut(next_action=N.STORED, reason='저장', quote_version_ids=['Q1'])
BUILD = PriorityOut(next_action=N.BUILD_PLANS, reason='진행')
PLANS = PlanOut(next_action=N.PLANS_PROPOSED, reason='구매안')
NODES = Nodes(preprocess=lambda s: {}, plan_validation=lambda s: {'plan_check': PlanCheck.PASS},
              price_validation=lambda s: {'price_check': PriceCheck.PASS}, review_card=lambda s: {'card_id': 'C1'})


def evidence(field):
    return [{'supplier_id': 'S1', 'field': field, 'mail_id': 'M1', 'source_text': f'{field} 원문'}]


class Script:
    """역할별 가짜 응답을 차례로 돌려준다. 호출 순서를 기록한다."""

    def __init__(self, answers):
        self.answers = {role: list(items) for role, items in answers.items()}
        self.calls = []

    def __call__(self, role, data):
        self.calls.append(role)
        return self.answers[role].pop(0)


def run(script):
    graph = build_graph(script, current_revision=lambda _: 1, nodes=NODES)
    return run_case(graph, RunCaseInput(case_id=uuid4(), start_from=StartFrom.REPLY, input_revision=1,
                                        mail_ids=['M1']))


class RoleCallLimitTests(unittest.TestCase):
    def test_limit_matches_the_database_limit(self):
        # DB begin_case_graph_agent_run(gyeonjwo_db 20261118043523)도 >=8에서 GRAPH_ROLE_LIMIT를 낸다
        self.assertEqual(MAX_ROLE_CALLS_PER_RUN, 8)

    def test_counter_offer_recheck_once_reaches_the_card(self):
        """f40a00e6 경로: ①③④⑤(RECHECK_SOURCE) 뒤 ①③④⑤를 다시 돌아 카드까지 간다(8회)."""
        recheck = CounterOfferOut(next_action=N.RECHECK_SOURCE, reason='인상 전 단가 재확인',
                                  unknown_fields=['previous_unit_price'], recheck=evidence('previous_unit_price'))
        skipped = CounterOfferOut(next_action=N.COUNTER_OFFER_SKIPPED, reason='역제안 불필요')
        script = Script({I: [STORED, STORED], P: [BUILD, BUILD], R: [PLANS, PLANS], C: [recheck, skipped]})
        result = run(script)
        self.assertEqual(script.calls, [I, P, R, C, I, P, R, C])
        self.assertEqual(result.status, RunStatus.COMPLETE)
        self.assertNotIn('ROLE_CALL_LIMIT', result.unresolved)

    def test_counter_offer_needs_evidence_once_reaches_the_card(self):
        """11-17 764924ab·f3cfece0 경로: ⑤ NEEDS_EVIDENCE → ③④⑤ (7회)."""
        needs = CounterOfferOut(next_action=N.NEEDS_EVIDENCE, reason='비용 확인', unknown_fields=['cost:SHIPPING'])
        skipped = CounterOfferOut(next_action=N.COUNTER_OFFER_SKIPPED, reason='역제안 불필요')
        script = Script({I: [STORED], P: [BUILD, BUILD], R: [PLANS, PLANS], C: [needs, skipped]})
        result = run(script)
        self.assertEqual(script.calls, [I, P, R, C, P, R, C])
        self.assertEqual(result.status, RunStatus.COMPLETE)

    def test_ninth_call_fails_with_role_call_limit_and_logs_it(self):
        rechecks = [PriorityOut(next_action=N.RECHECK_SOURCE, reason='재확인', unknown_fields=[f'cost:F{n}'],
                                recheck=evidence(f'cost:F{n}')) for n in range(1, 6)]
        script = Script({I: [STORED] * 6, P: rechecks})
        with self.assertLogs('gyeonjwo', level='WARNING') as logs:
            result = run(script)
        self.assertEqual(len(script.calls), MAX_ROLE_CALLS_PER_RUN)
        self.assertEqual(result.status, RunStatus.FAILED)
        self.assertIn('ROLE_CALL_LIMIT', result.unresolved)
        self.assertTrue(any('graph role call limit' in line and 'case_id=' in line for line in logs.output))


class SaveRejected(Exception):
    """FAILED 저장이 거절될 때 올리는 예외. 원래 예외(완료 저장 실패)와 구별하려고 따로 둔다."""


class Runs:
    """agent_runs 저장 가짜. finish 호출을 기록하고, 정해진 status에서 예외를 올릴 수 있다.
    COMPLETED 실패는 ServiceError(PROCESSING_UNAVAILABLE), FAILED 실패는 SaveRejected로 서로 다르다."""

    def __init__(self, fail_on=()):
        self.fail_on, self.finished, self.raised = set(fail_on), [], []

    def start_agent_run(self, *args):
        return {'agent_run_id': 'run-1', 'review_batch_id': 'batch-1'}

    def finish_agent_run(self, agent_run_id, status, output, error):
        self.finished.append((status, error))
        if status in self.fail_on:
            error = (SaveRejected('terminal role immutable') if status == 'FAILED' else
                     ServiceError('PROCESSING_UNAVAILABLE', 'rpc failed', 503, cause={'db_sqlstate': '40001'}))
            self.raised.append(error)
            raise error


def runner(runs, output=PlanOut(next_action=N.HOLD, reason='S1 보류')):
    instance = AgentRunner(model=object(), runs=runs)
    instance.agent_for = lambda role, data, channels=None: object()
    instance._run_once = lambda *args: (output, [])
    instance._verified = lambda role, handles, out, messages, run: out
    return instance


PLAN_IN = PlanIn(case_id=uuid4(), input_revision=1, quote_version_ids=['Q1'])


class PostprocessFailureTests(unittest.TestCase):
    def test_supplier_name_failure_is_saved_with_its_stage(self):
        runs = Runs()
        with mock.patch.object(agents_module, 'with_supplier_names', side_effect=ValueError('bad output')):
            with self.assertRaises(ValueError):
                runner(runs)(R, PLAN_IN)
        self.assertEqual(runs.finished, [('FAILED', {'code': 'AGENT_FAILED', 'stage': 'SUPPLIER_NAMES',
                                                     'error_type': 'ValueError', 'tool_calls': 0})])

    def test_completed_save_failure_is_saved_as_failed_with_finish_stage(self):
        runs = Runs(fail_on={'COMPLETED'})
        with mock.patch.object(agents_module, 'with_supplier_names', side_effect=lambda out, *a: out):
            with self.assertRaises(ServiceError):
                runner(runs)(R, PLAN_IN)
        status, error = runs.finished[-1]
        self.assertEqual((status, error['stage'], error['error_code'], error['db_sqlstate']),
                         ('FAILED', 'FINISH', 'PROCESSING_UNAVAILABLE', '40001'))

    def test_failed_save_failure_keeps_the_original_error(self):
        runs = Runs(fail_on={'COMPLETED', 'FAILED'})
        with mock.patch.object(agents_module, 'with_supplier_names', side_effect=lambda out, *a: out):
            with self.assertLogs('gyeonjwo', level='WARNING') as logs, self.assertRaises(Exception) as raised:
                runner(runs)(R, PLAN_IN)
        completed_error, save_error = runs.raised
        self.assertIs(raised.exception, completed_error)      # 저장 실패(SaveRejected)가 원래 예외를 덮지 않는다
        self.assertIsInstance(save_error, SaveRejected)
        self.assertEqual([status for status, _ in runs.finished], ['COMPLETED', 'FAILED'])
        self.assertTrue(any('agent failure not saved' in line and 'stage=FINISH' in line and 'run=run-1' in line
                            and 'save_error=SaveRejected' in line for line in logs.output))

    def test_model_failure_keeps_the_summary_stage(self):
        runs = Runs()
        instance = runner(runs)
        instance._run_once = mock.Mock(side_effect=RuntimeError('model down'))
        with self.assertRaises(RuntimeError):
            instance(R, PLAN_IN)
        self.assertEqual(runs.finished, [('FAILED', {'code': 'AGENT_FAILED', 'stage': 'OTHER',
                                                     'error_type': 'RuntimeError', 'tool_calls': 0})])

    def test_success_saves_completed_once(self):
        runs = Runs()
        with mock.patch.object(agents_module, 'with_supplier_names', side_effect=lambda out, *a: out):
            output = runner(runs)(R, PLAN_IN)
        self.assertEqual(output.next_action, N.HOLD)
        self.assertEqual([status for status, _ in runs.finished], ['COMPLETED'])


if __name__ == '__main__':
    unittest.main()
