import json
from uuid import UUID

import pytest
from agent_memory_store import MemoryStore
from test_agent_extractor import SYNTHETIC_CASES, synthetic_store
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
    other_case = load_evidence(store, CONTEXT, 'mail-9', 'body', None)
    assert other_case['status'] == 'NOT_FOUND' and 'available_parts' not in other_case   # 다른 업무 메일은 알려주지 않음
    assert load_evidence(store, CONTEXT, 'mail-1', 'att-1', 5)['status'] == 'NOT_FOUND'


def test_wrong_part_returns_the_parts_that_can_be_read(store):
    result = load_evidence(store, CONTEXT, 'mail-1', 'attachment_ref', 1)
    assert result['status'] == 'NOT_FOUND'
    assert result['available_parts'] == [{'part': 'body'}, {'part': 'att-1', 'pages': 2}]


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
        'interim': {'next_action': 'STORED', 'reason': '저장', 'unknown_fields': ['valid_until']},
        'source_quote_ids': ['qv-1'], 'source_run_ids': []}
    # 최종 결과는 Runner만 최상위에 쓴다. 도구의 중간 결과와 겹쳐 쓰지 않는다
    store.finish_agent_run('run-1', 'COMPLETED', {'next_action': 'HOLD', 'reason': '최종'}, None)
    output = store.agent_runs['run-1']['output_jsonb']
    assert (output['next_action'], output['interim']['next_action']) == ('HOLD', 'STORED')


def test_save_result_for_another_role_is_rejected_as_run_mismatch(store):
    started = store.start_agent_run(CASE, AgentRole.CANDIDATE, 3, {}, 'fake', 'p1')
    context = RunContext(CASE, 3, started['agent_run_id'], started['review_batch_id'])
    result = save_result(store, context, AgentRole.INFO_EXTRACTOR, 'STORED', '저장', [], [], [])
    assert result == {'status': 'REJECTED', 'code': 'RUN_MISMATCH'}         # revision 문제가 아니다
    assert store.agent_runs['run-1']['output_jsonb'] == {}
    missing = RunContext(CASE, 3, 'run-9', started['review_batch_id'])
    assert save_result(store, missing, AgentRole.INFO_EXTRACTOR, 'STORED', '저장', [], [], [])['code'] == 'RUN_MISMATCH'


def test_page_without_text_is_marked_empty(store):
    store.mail_messages['mail-1']['attachments'][0]['pages'][0] = '  '
    assert load_evidence(store, CONTEXT, 'mail-1', 'att-1', 1)['empty'] is True
    assert load_evidence(store, CONTEXT, 'mail-1', 'body', None)['empty'] is False


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


# ─────────────────────────────────────────────
# 합성 메일 20건으로 공통 도구 검사 (LLM 없음). 데이터는 test_agent_extractor.SYNTHETIC_CASES
# ─────────────────────────────────────────────

SYNTHETIC = [pytest.param(case.values[0], id=case.id) for case in SYNTHETIC_CASES]
MISSING_CASE = '00000000-0000-4000-8000-000000000099'


def synthetic_context(data):
    case_id, memory = synthetic_store(data)
    return memory, RunContext(case_id, 1)


def attachment_list(mail):
    return [{'attachment_ref': a['attachment_ref'], 'filename': a['filename'], 'pages': len(a['pages'])}
            for a in mail['attachments']]


@pytest.mark.parametrize('data', SYNTHETIC)
def test_case_context_returns_the_synthetic_case_and_its_mail_list(data):
    memory, context = synthetic_context(data)
    result = load_case_context(memory, context)
    assert result['status'] == 'OK' and result['input_revision'] == 1
    assert result['item_name'] == data['item_name'] and result['latest_quote_version_ids'] == []
    assert result['mandatory_conditions'] == data['case_fields'].get('mandatory_conditions')
    assert [(m['mail_id'], m['supplier_id'], m['subject'], m['received_at'], m['attachments'])
            for m in result['mails']] == [(m['id'], m['supplier_id'], m['subject'], m['received'], attachment_list(m))
                                          for m in data['mails']]
    assert all(mail['body'] not in json.dumps(result, ensure_ascii=False) for mail in data['mails'])   # 본문은 주지 않음


@pytest.mark.parametrize('data', SYNTHETIC)
def test_case_context_stops_when_revision_moved_or_case_is_unknown(data):
    memory, context = synthetic_context(data)
    assert load_case_context(memory, RunContext(MISSING_CASE, 1)) == {'status': 'CASE_NOT_FOUND'}
    memory.work_cases[context.case_id]['input_revision'] = 2           # 실행 중 새 회신이 들어옴
    assert load_case_context(memory, context) == {'status': 'REVISION_CHANGED'}


@pytest.mark.parametrize('data', SYNTHETIC)
def test_evidence_returns_every_body_and_attachment_page_as_written(data):
    memory, context = synthetic_context(data)
    for mail in data['mails']:
        body = load_evidence(memory, context, mail['id'], 'body', None)
        assert (body['status'], body['text'], body['location'], body['truncated']) == (
            'OK', mail['body'], f"{mail['id']} 본문", False)
        for attachment in mail['attachments']:
            for page, text in enumerate(attachment['pages'], 1):
                result = load_evidence(memory, context, mail['id'], attachment['attachment_ref'], page)
                assert (result['status'], result['text'], result['location']) == (
                    'OK', text, f"{mail['id']} {attachment['filename']} p.{page}")


@pytest.mark.parametrize('data', SYNTHETIC)
def test_wrong_part_or_page_tells_which_parts_can_be_read(data):
    memory, context = synthetic_context(data)
    for mail in data['mails']:
        readable = [{'part': 'body'}] + [{'part': a['attachment_ref'], 'pages': len(a['pages'])}
                                         for a in mail['attachments']]
        wrong = [('attachment_ref', 1), ('att-9', 1)] + [(a['attachment_ref'], len(a['pages']) + 1)
                                                         for a in mail['attachments']]
        for part, page in wrong:
            result = load_evidence(memory, context, mail['id'], part, page)
            assert result['status'] == 'NOT_FOUND' and result['available_parts'] == readable


@pytest.mark.parametrize('index', range(len(SYNTHETIC_CASES)))
def test_mails_of_another_synthetic_case_cannot_be_read(index):
    own, other = SYNTHETIC_CASES[index].values[0], SYNTHETIC_CASES[(index + 1) % len(SYNTHETIC_CASES)].values[0]
    case_id, memory = synthetic_store(own)
    memory.add_case(MISSING_CASE)
    for mail in other['mails']:
        memory.add_mail(f"other-{mail['id']}", MISSING_CASE, mail['body'], supplier_id=mail['supplier_id'],
                        attachments=mail['attachments'])
    context = RunContext(case_id, 1)
    assert [m['mail_id'] for m in load_case_context(memory, context)['mails']] == [m['id'] for m in own['mails']]
    for mail in other['mails']:
        for part, page in [('body', None)] + [(a['attachment_ref'], 1) for a in mail['attachments']]:
            result = load_evidence(memory, context, f"other-{mail['id']}", part, page)
            assert result['status'] == 'NOT_FOUND' and 'available_parts' not in result   # 다른 업무는 알려주지 않음


@pytest.mark.parametrize('data', SYNTHETIC)
def test_save_result_keeps_the_interim_result_apart_from_the_final_one(data):
    memory, context = synthetic_context(data)
    started = memory.start_agent_run(context.case_id, AgentRole.INFO_EXTRACTOR, 1, {}, 'fake', 'p1')
    context = RunContext(context.case_id, 1, started['agent_run_id'], started['review_batch_id'])
    reason = f"{data['mails'][0]['id']} 확인"

    def save(action='HOLD', why=reason):
        return save_result(memory, context, AgentRole.INFO_EXTRACTOR, action, why, ['unit_price'], ['qv-0'], [])
    assert save('SEND_RFQ') == {'status': 'REJECTED', 'code': 'INVALID_NEXT_ACTION'}   # ①이 고를 수 없는 값
    assert save(why=' ') == {'status': 'REJECTED', 'code': 'INVALID_REASON'}
    assert save(why='가' * 501) == {'status': 'REJECTED', 'code': 'INVALID_REASON'}
    assert memory.agent_runs['run-1']['output_jsonb'] == {}
    assert save() == {'status': 'SAVED', 'result_id': 'run-1', 'is_latest': True}
    assert memory.agent_runs['run-1']['output_jsonb'] == {
        'interim': {'next_action': 'HOLD', 'reason': reason, 'unknown_fields': ['unit_price']},
        'source_quote_ids': ['qv-0'], 'source_run_ids': []}
    memory.finish_agent_run('run-1', 'COMPLETED', {'next_action': 'STORED', 'reason': '최종'}, None)
    output = memory.agent_runs['run-1']['output_jsonb']
    assert (output['next_action'], output['interim']['next_action']) == ('STORED', 'HOLD')   # 서로 덮어쓰지 않음
    memory.work_cases[context.case_id]['input_revision'] = 2
    assert save('STORED', '다시') == {'status': 'REVISION_CHANGED', 'is_latest': False}
    assert memory.agent_runs['run-1']['output_jsonb']['interim']['reason'] == reason       # 바뀐 revision에선 안 씀


@pytest.mark.parametrize('data', SYNTHETIC)
def test_common_tools_called_by_the_agent_use_the_running_case_and_role(data):
    case_id, memory = synthetic_store(data)
    first = data['mails'][0]
    calls = [('get_case_context', {}), ('read_mail_evidence', {'mail_id': first['id']})]
    calls += [('read_mail_evidence', {'mail_id': first['id'], 'part': a['attachment_ref'], 'page': 1})
              for a in first['attachments']]
    calls += [('save_agent_result', {'next_action': 'HOLD', 'reason': '도구 확인', 'unknown_fields': ['unit']})]
    started = memory.start_agent_run(case_id, AgentRole.INFO_EXTRACTOR, 1, {}, 'fake', 'p1')
    tools.use_case_store(memory)
    try:
        result = build_agent(AgentRole.INFO_EXTRACTOR, FakeModel(messages=iter(messages(calls)))).invoke(
            {'messages': [('user', '{}')]},
            context=RunContext(case_id, 1, started['agent_run_id'], started['review_batch_id']))
    finally:
        tools.use_case_store(None)
    outputs = [json.loads(m.content) for m in result['messages']
               if isinstance(m, ToolMessage) and m.name != 'ExtractionOut']
    context_out, body, *pages, saved = outputs
    assert context_out['status'] == 'OK' and [m['mail_id'] for m in context_out['mails']] == [
        m['id'] for m in data['mails']]
    assert body['text'] == first['body']
    assert [page['text'] for page in pages] == [a['pages'][0] for a in first['attachments']]
    assert saved == {'status': 'SAVED', 'result_id': 'run-1', 'is_latest': True}
    # 역할은 에이전트 이름(info_extractor)에서 읽혀 이 실행 기록에 저장됐다
    assert memory.agent_runs['run-1']['output_jsonb']['interim'] == {
        'next_action': 'HOLD', 'reason': '도구 확인', 'unknown_fields': ['unit']}
