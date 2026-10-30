from uuid import uuid4

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage
from langchain_core.tools import tool

from app.agents import agents, tools
from app.agents.agents import AgentRunner, UngroundedOutputError
from app.agents.schemas import AgentRole, ExtractionIn, ExtractionOut, PriorityIn, PriorityOut


class FakeModel(GenericFakeChatModel):
    def bind_tools(self, tools, **kwargs):
        return self


def call(name, args, index):
    return AIMessage(content='', tool_calls=[{'name': name, 'args': args, 'id': f'c{index}'}])


@pytest.fixture
def registry(monkeypatch):
    calls = []

    @tool
    def read_mail_evidence(evidence_id: str) -> str:
        '''Read mail evidence.'''
        calls.append(evidence_id)
        return '원문'

    @tool
    def store_structured_mail(note: str) -> dict:
        '''Store a quote.'''
        return {'status': 'STORED', 'quote_version_id': 'q1'}

    monkeypatch.setattr(tools, '_registry', {})
    tools.register(read_mail_evidence)
    tools.register(store_structured_mail)
    return calls


def extraction_in():
    return ExtractionIn(case_id=uuid4(), input_revision=1, mail_ids=['m1'])


def test_agent_calls_allowed_tool_then_returns_validated_output(registry):
    model = FakeModel(messages=iter([
        call('read_mail_evidence', {'evidence_id': 'm1'}, 1),
        call('store_structured_mail', {'note': 'q'}, 2),
        call('ExtractionOut', {'next_action': 'STORED', 'reason': '저장', 'quote_version_ids': ['q1']}, 3)]))
    out = AgentRunner(model)(AgentRole.INFO_EXTRACTOR, extraction_in())
    assert isinstance(out, ExtractionOut) and out.quote_version_ids == ['q1']
    assert registry == ['m1']


def test_tool_call_limit_raises(registry):
    model = FakeModel(messages=iter([call('read_mail_evidence', {'evidence_id': f'm{i}'}, i) for i in range(20)]))
    with pytest.raises(Exception):
        AgentRunner(model)(AgentRole.INFO_EXTRACTOR, extraction_in())
    assert len(registry) == 8


def test_wrong_input_model_is_rejected():
    with pytest.raises(TypeError):
        AgentRunner(FakeModel(messages=iter([])))(AgentRole.INFO_PRIORITY, extraction_in())
    PriorityIn(case_id=uuid4(), input_revision=1, quote_version_ids=[])


def test_register_rejects_tools_outside_the_allowlist(monkeypatch):
    monkeypatch.setattr(tools, '_registry', {})

    @tool
    def run_sql(query: str) -> str:
        '''Run SQL.'''
        return ''

    with pytest.raises(ValueError):
        tools.register(run_sql)


class Runs:
    """실행 기록 저장소 대역. 시작·종료 상태만 남긴다."""

    def __init__(self):
        self.finished = {}

    def start_agent_run(self, case_id, role, input_revision, input_snapshot, model_id, prompt_version):
        return {'agent_run_id': 'run-1', 'review_batch_id': 'batch-1'}

    def finish_agent_run(self, agent_run_id, status, output, error):
        self.finished[agent_run_id] = (status, error)


def priority_model():
    return FakeModel(messages=iter([call('PriorityOut', {'next_action': 'BUILD_PLANS', 'reason': '검토 가능'}, 1)]))


def priority_in():
    return PriorityIn(case_id=uuid4(), input_revision=3, quote_version_ids=['q1'])


def test_registered_final_check_receives_input_and_run_context(registry, monkeypatch):
    seen = {}

    def check(role, data, context, output, messages):
        seen.update(role=role, data=data, context=context, messages=messages)
        return output.model_copy(update={'reason': '검사 통과'})

    monkeypatch.setitem(agents.FINAL_CHECKS, AgentRole.INFO_PRIORITY, check)
    data = priority_in()
    out = AgentRunner(priority_model(), Runs())(AgentRole.INFO_PRIORITY, data)
    assert out.reason == '검사 통과'
    assert seen['role'] == AgentRole.INFO_PRIORITY and seen['data'] is data
    assert seen['context'].case_id == str(data.case_id) and seen['context'].agent_run_id == 'run-1'
    assert seen['messages']


def test_failed_final_check_closes_run_as_failed(registry, monkeypatch):
    def check(role, data, context, output, messages):
        raise UngroundedOutputError('calculation_ref was not issued in this run')

    monkeypatch.setitem(agents.FINAL_CHECKS, AgentRole.INFO_PRIORITY, check)
    runs = Runs()
    with pytest.raises(UngroundedOutputError):
        AgentRunner(priority_model(), runs)(AgentRole.INFO_PRIORITY, priority_in())
    assert runs.finished['run-1'] == ('FAILED', {'type': 'UngroundedOutputError'})


def test_final_check_must_return_the_roles_output_model(registry, monkeypatch):
    monkeypatch.setitem(agents.FINAL_CHECKS, AgentRole.INFO_PRIORITY,
                        lambda role, data, context, output, messages: ExtractionOut(next_action='HOLD', reason='x'))
    runs = Runs()
    with pytest.raises(TypeError):
        AgentRunner(priority_model(), runs)(AgentRole.INFO_PRIORITY, priority_in())
    assert runs.finished['run-1'][0] == 'FAILED'


def test_role_without_final_check_keeps_validated_output(registry, monkeypatch):
    monkeypatch.delitem(agents.FINAL_CHECKS, AgentRole.INFO_PRIORITY, raising=False)
    runs = Runs()
    out = AgentRunner(priority_model(), runs)(AgentRole.INFO_PRIORITY, priority_in())
    assert isinstance(out, PriorityOut) and out.next_action == 'BUILD_PLANS'
    assert runs.finished['run-1'] == ('COMPLETED', None)


def test_extractor_check_is_registered():
    assert agents.FINAL_CHECKS[AgentRole.INFO_EXTRACTOR] is agents.check_extraction
