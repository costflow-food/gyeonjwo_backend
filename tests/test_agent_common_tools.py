from uuid import UUID

import pytest
from agent_memory_store import MemoryStore
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, ToolMessage

from app.agents import tools
from app.agents.agents import AgentRunner, build_agent
from app.agents.schemas import AgentRole, ExtractionIn, RunContext
from app.agents.tools import (MAX_EVIDENCE_CHARS, get_case_context, load_case_context, load_evidence,
                              read_mail_evidence, save_agent_result, save_result)

CASE = '6f1c2a54-2f0e-4c1b-9a51-7d1e2b3c4d5e'
OTHER_CASE = '0b9e8d7c-6a5b-4c3d-8e2f-1a0b9c8d7e6f'
CONTEXT = RunContext(CASE, 3)


@pytest.fixture
def store():
    memory = MemoryStore()
    memory.add_case(CASE, input_revision=3, item_name='냉동 딸기', mandatory_conditions={'certifications': ['HACCP']},
                    policy={'currency': 'KRW', 'allow_split': True}, owner_user_id='secret')
    memory.add_case(OTHER_CASE)
    memory.add_mail('mail-1', CASE, '가나농산 냉동 딸기 5,450원/kg', supplier_id='sup-ganna', attachments=[
        {'attachment_ref': 'att-1', 'filename': 'quote.pdf', 'pages': ['표지', '단가표']}])
    memory.add_mail('mail-9', OTHER_CASE, '다른 업무 원문')
    tools.use_case_store(memory)
    yield memory
    tools.use_case_store(None)


def test_case_context_returns_work_case_and_mail_list_without_bodies(store):
    result = load_case_context(store, CONTEXT)
    assert result['status'] == 'OK' and result['input_revision'] == 3 and result['item_name'] == '냉동 딸기'
    assert result['mandatory_conditions'] == {'certifications': ['HACCP']}
    assert result['policy'] == {'currency': 'KRW', 'allow_split': True}
    assert result['mails'] == [{'mail_id': 'mail-1', 'message_role': 'INITIAL', 'direction': 'IN',
                                'supplier_id': 'sup-ganna', 'received_at': '2026-10-29T09:00:00+09:00',
                                'subject': 'mail-1 제목',
                                'attachments': [{'attachment_ref': 'att-1', 'filename': 'quote.pdf', 'pages': 2}]}]
    assert 'owner_user_id' not in result and 'body_text' not in str(result)


@pytest.mark.parametrize('context, status', [(RunContext(CASE, 2), 'REVISION_CHANGED'),
                                             (RunContext('nope', 3), 'CASE_NOT_FOUND')])
def test_case_context_stops_on_stale_or_unknown_case(store, context, status):
    assert load_case_context(store, context) == {'status': status}


def test_read_is_retried_once_then_raises(store):
    store.read_failures = 1
    assert load_case_context(store, CONTEXT)['status'] == 'OK'
    store.read_failures = 2
    with pytest.raises(ConnectionError):
        load_case_context(store, CONTEXT)


def test_evidence_reads_body_or_attachment_page_of_the_running_case_only(store):
    assert load_evidence(store, CONTEXT, 'mail-1', 'body', None)['text'] == '가나농산 냉동 딸기 5,450원/kg'
    page = load_evidence(store, CONTEXT, 'mail-1', 'att-1', 2)
    assert (page['text'], page['location'], page['truncated']) == ('단가표', 'mail-1 quote.pdf p.2', False)
    assert load_evidence(store, CONTEXT, 'mail-9', 'body', None)['status'] == 'NOT_FOUND'
    assert load_evidence(store, CONTEXT, 'mail-1', 'att-1', 5)['status'] == 'NOT_FOUND'


def test_long_evidence_is_truncated(store):
    store.mail_messages['mail-1']['body_text'] = '가' * (MAX_EVIDENCE_CHARS + 10)
    result = load_evidence(store, CONTEXT, 'mail-1', 'body', None)
    assert len(result['text']) == MAX_EVIDENCE_CHARS and result['truncated'] is True


def test_save_result_writes_agent_run_output(store):
    started = store.start_agent_run(CASE, AgentRole.INFO_EXTRACTOR, 3, {}, 'fake', 'p1')
    context = RunContext(CASE, 3, started['agent_run_id'], started['review_batch_id'])

    def save(action, reason='저장', ctx=context):
        return save_result(store, ctx, AgentRole.INFO_EXTRACTOR, action, reason, ['valid_until'], ['qv-1'], [])
    assert save('SEND_RFQ') == {'status': 'REJECTED', 'code': 'INVALID_NEXT_ACTION'}
    assert save('STORED', '  ') == {'status': 'REJECTED', 'code': 'INVALID_REASON'}
    assert save('STORED', ctx=RunContext(CASE, 2, 'run-1')) == {'status': 'REVISION_CHANGED', 'is_latest': False}
    assert store.agent_runs['run-1']['output_jsonb'] == {}
    assert save('STORED') == {'status': 'SAVED', 'result_id': 'run-1', 'is_latest': True}
    assert store.agent_runs['run-1']['output_jsonb'] == {
        'next_action': 'STORED', 'reason': '저장', 'unknown_fields': ['valid_until'], 'source_quote_ids': ['qv-1'],
        'source_run_ids': []}


def test_save_result_needs_a_recorded_run(store):
    with pytest.raises(RuntimeError, match='not recorded'):
        save_result(store, CONTEXT, AgentRole.INFO_EXTRACTOR, 'STORED', '저장', [], [], [])


def test_llm_cannot_pass_case_revision_run_or_role():
    def visible(t):
        return set(t.tool_call_schema.model_json_schema().get('properties', {}))
    assert visible(get_case_context) == set()
    assert visible(read_mail_evidence) == {'mail_id', 'part', 'page'}
    assert visible(save_agent_result) == {'next_action', 'reason', 'unknown_fields', 'source_quote_ids',
                                          'source_run_ids'}


class FakeModel(GenericFakeChatModel):
    def bind_tools(self, tools, **kwargs):
        return self


def messages(calls):
    out = [AIMessage(content='', tool_calls=[{'name': name, 'args': args, 'id': f'c{index}'}])
           for index, (name, args) in enumerate(calls)]
    return out + [AIMessage(content='', tool_calls=[{'name': 'ExtractionOut', 'id': 'end',
                                                     'args': {'next_action': 'HOLD', 'reason': '확인'}}])]


def extraction_in():
    return ExtractionIn(case_id=UUID(CASE), input_revision=3, mail_ids=['mail-1'])


def test_runner_records_run_and_tools_use_run_context_and_role(store):
    model = FakeModel(messages=iter(messages([
        ('get_case_context', {}), ('read_mail_evidence', {'mail_id': 'mail-9'}),
        ('save_agent_result', {'next_action': 'HOLD', 'reason': '견적 없음'})])))
    out = AgentRunner(model, runs=store)(AgentRole.INFO_EXTRACTOR, extraction_in())
    assert out.next_action == 'HOLD'
    run = store.agent_runs['run-1']
    assert (run['work_case_id'], run['review_batch_id'], run['agent_role'], run['status']) == (
        CASE, 'batch-6f1c', AgentRole.INFO_EXTRACTOR, 'COMPLETED')
    assert run['input_snapshot_jsonb']['mail_ids'] == ['mail-1'] and len(run['prompt_version']) == 12
    assert run['output_jsonb']['reason'] == '확인'          # Runner가 최종 응답을 합침


def test_tool_results_inside_the_agent(store):
    model = FakeModel(messages=iter(messages([('get_case_context', {}),
                                              ('read_mail_evidence', {'mail_id': 'mail-9'})])))
    result = build_agent(AgentRole.INFO_EXTRACTOR, model).invoke(
        {'messages': [('user', extraction_in().model_dump_json())]}, context=CONTEXT)
    outputs = [m.content for m in result['messages'] if isinstance(m, ToolMessage)]
    assert '"status": "OK"' in outputs[0] and '"NOT_FOUND"' in outputs[1]


def test_runner_records_failure(store):
    model = FakeModel(messages=iter([AIMessage(content='', tool_calls=[
        {'name': 'save_agent_result', 'args': {'next_action': 'STORED', 'reason': 'x'}, 'id': 'c1'}])]))
    tools.use_case_store(None)
    with pytest.raises(RuntimeError):
        AgentRunner(model, runs=store)(AgentRole.INFO_EXTRACTOR, extraction_in())
    assert store.agent_runs['run-1']['status'] == 'FAILED'
    assert store.agent_runs['run-1']['error_jsonb'] == {'type': 'RuntimeError'}
