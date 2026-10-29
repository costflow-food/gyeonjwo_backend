from uuid import uuid4

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage
from langchain_core.tools import tool

from app.agents import tools
from app.agents.agents import AgentRunner
from app.agents.schemas import AgentRole, ExtractionIn, ExtractionOut, PriorityIn


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
