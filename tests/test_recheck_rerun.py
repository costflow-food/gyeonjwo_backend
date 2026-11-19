"""③④ 재추출 근거 오류 뒤 새 대화로 한 번 다시 실행 (G23).

평가 eval-1117-fix에서 ③ 개발 #02(이미 0원으로 저장된 운송비를 재추출)·#08(다른 업체 메일의 납품일 인용)이
5번 중 1번 최종 검사(UngroundedOutputError)로 실패했다. 같은 대화에서 고칠 점을 알려도 LLM이 같은 답을 되풀이해,
초안을 만들지 않은 실행이면 새 대화로 한 번 다시 한다.
"""
import unittest
from unittest import mock
from uuid import uuid4

from app.agents import agents
from app.agents.agents import AgentRunner, UngroundedOutputError, UngroundedRecheckError
from app.agents.schemas import AgentNextAction as N, AgentRole, PriorityIn, PriorityOut

BUILD = PriorityOut(next_action=N.BUILD_PLANS, reason='검토 가능')


def priority_input():
    """③ 입력 하나."""
    return PriorityIn(case_id=uuid4(), input_revision=1, quote_version_ids=['Q1'])


def runner_with(results, drafted_on_first=False):
    """_run_once가 results를 차례로 돌려주거나(예외면 올린다) 하는 실행기와 호출 기록."""
    calls = []

    def fake_run_once(role, agent, data, context, messages, log):
        """가짜 실행: 첫 실행에서 초안 도구를 불렀다고 기록할 수 있다."""
        calls.append(messages)
        if drafted_on_first and len(calls) == 1:
            log.calls.append({'tool': 'create_clarification_draft', 'args': {}})
        result = results[len(calls) - 1]
        if isinstance(result, Exception):
            raise result
        return result, []

    runner = AgentRunner(model=mock.Mock())
    patches = [mock.patch.object(AgentRunner, 'agent_for', return_value=None),
               mock.patch.object(AgentRunner, '_run_once', staticmethod(fake_run_once)),
               mock.patch.object(agents, 'with_supplier_names', lambda output, handles, case_id: output)]
    return runner, calls, patches


class RecheckRerunTests(unittest.TestCase):
    def run_priority(self, results, drafted_on_first=False):
        """③을 가짜 실행 결과로 부르고 (결과 또는 예외, 실행 횟수)를 돌려준다."""
        runner, calls, patches = runner_with(results, drafted_on_first)
        for patch in patches:
            patch.start()
        self.addCleanup(mock.patch.stopall)
        try:
            return runner(AgentRole.INFO_PRIORITY, priority_input()), len(calls)
        except Exception as error:
            return error, len(calls)

    def test_recheck_error_runs_once_more_from_a_new_conversation(self):
        output, runs = self.run_priority([UngroundedRecheckError('S1.cost:SHIPPING is not missing'), BUILD])
        self.assertEqual((output.next_action, runs), (N.BUILD_PLANS, 2))

    def test_second_recheck_error_fails_the_run(self):
        error, runs = self.run_priority([UngroundedRecheckError('first'), UngroundedRecheckError('second')])
        self.assertIsInstance(error, UngroundedRecheckError)
        self.assertEqual((str(error), runs), ('second', 2))

    def test_run_that_saved_a_draft_is_not_repeated(self):
        error, runs = self.run_priority([UngroundedRecheckError('first'), BUILD], drafted_on_first=True)
        self.assertIsInstance(error, UngroundedRecheckError)
        self.assertEqual(runs, 1)

    def test_other_final_check_errors_are_not_repeated(self):
        error, runs = self.run_priority([UngroundedOutputError('draft_ids were not drafted'), BUILD])
        self.assertNotIsInstance(error, UngroundedRecheckError)
        self.assertIsInstance(error, UngroundedOutputError)
        self.assertEqual(runs, 1)

    def test_recheck_error_is_a_final_check_error(self):
        self.assertTrue(issubclass(UngroundedRecheckError, UngroundedOutputError))


if __name__ == '__main__':
    unittest.main()
