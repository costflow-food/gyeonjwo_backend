"""실행기 배선 점검 (API 호출 없음). 정해진 도구 호출을 차례로 내는 가짜 모델로 실제 AgentRunner·도구·저장소·기록이
한 바퀴 도는지 본다. 모델 품질과는 상관없다.

    python -m scripts.agent_eval.smoke
"""
from __future__ import annotations

import json
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from scripts.agent_eval import run as runner_module


class ScriptedModel(BaseChatModel):
    """script의 (도구 이름, 인자)를 한 턴에 하나씩 tool call로 낸다."""
    script: list[tuple[str, dict]]
    step: int = 0

    @property
    def _llm_type(self) -> str:
        return 'scripted'

    def bind_tools(self, tools: Any, **kwargs: Any):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        name, args = self.script[min(self.step, len(self.script) - 1)]
        self.step += 1
        message = AIMessage(content='', tool_calls=[{'name': name, 'args': args, 'id': f'call-{self.step}', 'type': 'tool_call'}],
                            usage_metadata={'input_tokens': 100, 'output_tokens': 10, 'total_tokens': 110})
        return ChatResult(generations=[ChatGeneration(message=message)])


SCRIPTS = {
    'C-50': {'CANDIDATE': [('get_case_context', {}),
                           ('CandidateOut', {'next_action': 'HOLD', 'reason': '실행 중 업무 정보가 바뀌어 멈췄습니다.'})]},
    'P-05': {'INFO_PRIORITY': [('get_case_context', {}), ('check_comparability', {}),
                               ('PriorityOut', {'next_action': 'BUILD_PLANS', 'reason': '비교에 필요한 값이 모두 확인됐습니다.'})]},
}


def main():
    def fake_models(options, usage):
        from app.agents.schemas import AgentRole
        script = SCRIPTS[options['scenario']]
        models = {role: ScriptedModel(script=script.get(str(role), [('noop', {})]), callbacks=[usage.handler]) for role in AgentRole}
        return models, {}

    runner_module.make_models = fake_models
    for scenario_id in SCRIPTS:
        record = runner_module.run_one({'id': scenario_id, 'repeat': 1,
                                        'options': {'model': None, 'verifier_model': None, 'no_verifier': True,
                                                    'dry_plan': False, 'scenario': scenario_id}})
        print(scenario_id, record['status'], (record.get('output') or {}).get('next_action'), record.get('error', ''))
        print('  도구:', [t['tool'] for t in record['tools']], '· 토큰:', record['usage'])
        from scripts.agent_eval.grade import grade_record
        print('  채점:', json.dumps(grade_record(record), ensure_ascii=False))


if __name__ == '__main__':
    main()
