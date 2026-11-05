import json
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import pytest
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, ToolMessage

from app.agents import agents, tools
from app.agents.schemas import AgentRole, CandidateOut, RunContext
from app.features.work_cases.domain.candidates import CandidateSnapshot
from app.features.work_cases.application.candidates import CandidateService
from app.features.work_cases.infrastructure.graph_store import GraphStore
from app.features.supplier_crawl.infrastructure import google_search
from app.features.supplier_crawl.infrastructure import network


class ToolCallingFake(FakeMessagesListChatModel):
    def bind_tools(self, bound_tools, **kwargs):
        return self


def test_network_connects_only_to_validated_public_address(monkeypatch):
    sock = Mock()
    sock.getpeername.return_value = ('93.184.216.34', 443)
    lookup = Mock(return_value=[(2, 1, 6, '', ('93.184.216.34', 443))])
    monkeypatch.setattr(network.socket, 'getaddrinfo', lookup)
    monkeypatch.setattr(network.socket, 'socket', lambda *args: sock)
    stream = network._PublicBackend().connect_tcp('example.test', 443, timeout=2)
    sock.connect.assert_called_once_with(('93.184.216.34', 443))
    assert lookup.call_count == 1
    stream.close()


def test_network_rejects_mixed_public_and_private_dns(monkeypatch):
    monkeypatch.setattr(network.socket, 'getaddrinfo', lambda *args, **kwargs: [
        (2, 1, 6, '', ('93.184.216.34', 443)), (2, 1, 6, '', ('127.0.0.1', 443))])
    with pytest.raises(network.UnsafeAddress):
        network.resolve_public('example.test', 443)


def test_container_uses_installed_chromium(monkeypatch):
    from scrapling.fetchers import StealthyFetcher
    monkeypatch.setattr(google_search, 'which', lambda name: '/usr/bin/chromium' if name == 'chromium' else None)
    fetch = Mock(return_value=SimpleNamespace(status=200, body=b'<html></html>', url='https://www.google.com/search'))
    monkeypatch.setattr(StealthyFetcher, 'fetch', fetch)
    assert google_search._search_page('https://www.google.com/search')[0] == b'<html></html>'
    assert fetch.call_args.kwargs['executable_path'] == '/usr/bin/chromium'


def test_candidate_agent_calls_scoped_collector(monkeypatch):
    context = RunContext(str(uuid4()), 1)
    monkeypatch.setattr(tools, '_case_store', SimpleNamespace(get_work_case=lambda _: {'input_revision': 1}))
    monkeypatch.setattr(google_search, 'search_suppliers', lambda query, limit: {'status': 'ERROR', 'code': 'SEARCH_BLOCKED', 'suppliers': []})
    model = ToolCallingFake(responses=[
        AIMessage(content='', tool_calls=[{'name': 'crawl_supplier_candidates', 'args': {'query': '냉동망고'}, 'id': 'call_1'}]),
        AIMessage(content='', tool_calls=[{'name': 'CandidateOut', 'args': {'next_action': 'HOLD', 'reason': '웹 검색이 차단됐습니다.'}, 'id': 'out_1'}]),
    ])
    result = agents.build_agent(AgentRole.CANDIDATE, model).invoke(
        {'messages': [('user', '냉동망고 공급처를 찾아줘')]}, context=context)
    checked = agents._check_candidates(result['structured_response'], result['messages'])
    assert checked.next_action == 'HOLD'
    assert checked.web_observations[0]['code'] == 'SEARCH_BLOCKED'
    assert checked.candidates == [] and checked.draft_ids == []


def test_candidate_registry_and_revision_checked(monkeypatch):
    assert 'crawl_supplier_candidates' in {t.name for t in tools.tools_for(AgentRole.CANDIDATE)}
    assert all('crawl_supplier_candidates' not in {t.name for t in tools.tools_for(role)}
               for role in AgentRole if role != AgentRole.CANDIDATE)
    case_id = str(uuid4())
    context = RunContext(case_id, 1)
    runtime = SimpleNamespace(context=context)
    store = SimpleNamespace(get_work_case=lambda _: {'input_revision': 1})
    monkeypatch.setattr(tools, '_case_store', store)
    calls = []
    monkeypatch.setattr(google_search, 'search_suppliers', lambda query, limit: calls.append((query, limit)) or {'status': 'EMPTY', 'suppliers': []})
    assert tools.crawl_supplier_candidates.func('냉동망고', runtime)['status'] == 'EMPTY'
    assert calls == [('냉동망고', 5)]
    store.get_work_case = lambda _: {'input_revision': 2}
    assert tools.crawl_supplier_candidates.func('냉동망고', runtime)['status'] == 'REVISION_CHANGED'
    assert len(calls) == 1


def test_no_registered_candidates_does_not_skip_web_discovery(monkeypatch):
    runtime = SimpleNamespace(context=RunContext(str(uuid4()), 1))
    monkeypatch.setattr(tools, '_candidate_store', object())
    monkeypatch.setattr(tools, 'evaluate_candidates', lambda *_: {
        'status': 'OK', 'candidates': [], 'next': '바로 HOLD한다.'})
    result = tools.search_supplier_evidence.func([], runtime)
    assert 'crawl_supplier_candidates' in result['next']


def test_revision_change_after_search_discards_web_results(monkeypatch):
    state = iter([{'input_revision': 1}, {'input_revision': 2}])
    monkeypatch.setattr(tools, '_case_store', SimpleNamespace(get_work_case=lambda _: next(state)))
    monkeypatch.setattr(google_search, 'search_suppliers', lambda query, limit: {'status': 'OK', 'suppliers': [{'search_title': 'unverified'}]})
    result = tools.crawl_supplier_candidates.func('냉동망고', SimpleNamespace(context=RunContext(str(uuid4()), 1)))
    assert result == {'status': 'REVISION_CHANGED', 'suppliers': []}


def test_web_results_are_separate_and_grounded_in_tool_output():
    observed = {'status': 'OK', 'query': '냉동망고', 'suppliers': [{'search_title': '검색 제목', 'homepage_url': 'https://example.test/'}]}
    msg = ToolMessage(content=json.dumps(observed), name='crawl_supplier_candidates', tool_call_id='call_1')
    output = CandidateOut(next_action='HOLD', reason='등록 후보가 없어 웹을 검색했습니다.',
                          web_observations=[{'status': 'invented'}])
    checked = agents._check_candidates(output, [msg])
    assert checked.candidates == [] and checked.draft_ids == []
    assert checked.web_observations == [observed]
    snapshot = CandidateSnapshot(case_id=uuid4(), candidate_set_id=uuid4(), revision=1,
                                 review_batch_id=uuid4(), items=[], web_observations=checked.web_observations)
    assert CandidateSnapshot.model_validate_json(snapshot.model_dump_json()).web_observations == [observed]
    store = GraphStore.__new__(GraphStore)
    store.scope = SimpleNamespace(case_id=str(snapshot.case_id), revision=1, batch_id=str(snapshot.review_batch_id))
    saved = store.candidate_snapshot(str(snapshot.candidate_set_id), checked.model_dump(mode='json'))
    assert saved.items == [] and saved.web_observations == [observed]


def test_blocked_search_remains_distinct_from_empty():
    blocked = {'status': 'ERROR', 'code': 'SEARCH_BLOCKED', 'suppliers': []}
    msg = ToolMessage(content=json.dumps(blocked), name='crawl_supplier_candidates', tool_call_id='call_1')
    result = agents._check_candidates(CandidateOut(next_action='HOLD', reason='검색이 차단됐습니다.'), [msg])
    assert result.web_observations == [blocked] and result.candidates == []


def test_unregistered_web_supplier_cannot_become_selectable():
    case_id, owner, batch_id, run_id = uuid4(), uuid4(), uuid4(), uuid4()
    observation = {'status': 'OK', 'suppliers': [{'search_title': '웹 검색', 'homepage_url': 'https://example.test/'}]}
    snapshot = CandidateSnapshot(case_id=case_id, candidate_set_id=run_id, revision=1,
                                 review_batch_id=batch_id, items=[], web_observations=[observation])
    repo = SimpleNamespace(
        case=lambda *_: {'input_revision': 1, 'current_batch_id': str(batch_id),
                         'status': 'Pending', 'processing_step': 'ON_HOLD'},
        latest=lambda *_: snapshot, selection=lambda *_: None,
        ready=lambda: SimpleNamespace(available=True))
    response = CandidateService(repo, policy_repo=SimpleNamespace()).get(owner, case_id)
    assert response.web_observations == [observation]
    assert response.items == [] and 'SELECT_CANDIDATES' not in response.allowed_actions
    with pytest.raises(agents.UngroundedOutputError):
        agents._check_candidates(CandidateOut(next_action='SEND_RFQ', reason='허위 초안',
                                 draft_ids=['invented']), [])
