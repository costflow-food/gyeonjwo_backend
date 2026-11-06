from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import pytest

from app.agents.schemas import CandidateOut, RunContext
from app.agents.tools import EvidenceRef, QuoteField, save_structured_quote
from app.features.work_cases.domain.hold import from_agent_output
from app.features.work_cases.infrastructure.graph_store import GraphStore


def graph_store(role):
    case_id, mail_id, batch_id, run_id = (str(uuid4()) for _ in range(4))
    repo = SimpleNamespace(call=Mock(return_value=True))
    scope = SimpleNamespace(case_id=case_id, mail_ids=[mail_id], batch_id=batch_id,
                            revision=1, repo=repo, check=Mock())
    store = GraphStore(scope, {'job_id': 'job', 'claim_token': 'claim'})
    store.role_runs[run_id] = role
    return store, run_id


@pytest.mark.parametrize('linked', [False, True])
def test_supplier_rejection_records_one_scoped_rpc_and_finishes_hold(linked):
    store, run_id = graph_store('INFO_EXTRACTOR')
    scope = store.scope
    mail_id = scope.mail_ids[0]
    store.active_extraction_run = run_id
    store.get_work_case = Mock(return_value={'input_revision': scope.revision})
    store.list_case_mails = Mock(return_value=[
        {'mail_id': mail_id, 'supplier_id': 'linked-supplier' if linked else None},
    ])
    store.read_mail_part = Mock(return_value={'text': '가나농산 주식회사', 'location': 'body'})
    store.resolve_supplier = Mock(return_value='other-supplier' if linked else None)

    fields = [
        QuoteField(field_name='supplier_name', raw_value='가나농산 주식회사', evidence=EvidenceRef(mail_id=mail_id)),
        QuoteField(field_name='specification', raw_value='없는 규격'),
    ]
    result = save_structured_quote(store, RunContext(scope.case_id, scope.revision, run_id), fields, [], [], [])
    code = 'SUPPLIER_MISMATCH' if linked else 'SUPPLIER_NOT_MATCHED'
    assert result['rejected'] == [{'field': 'supplier_name', 'code': code}]
    if not linked:
        assert '한 번 더' in result['next']
    store.repo.call.assert_called_once_with(
        'record_case_graph_extraction_issue', p_job='job', p_token='claim', p_revision=1, p_run=run_id,
        p_issue={'case_id': scope.case_id, 'mail_id': mail_id, 'batch_id': scope.batch_id,
                 'input_revision': 1, 'code': code, 'field': 'supplier_name', 'supplier_name': '가나농산 주식회사'},
    )

    store.finish_agent_run(run_id, 'COMPLETED', {
        'next_action': 'HOLD', 'reason': '업체명이 없어요.',
        'hold': {'code': 'MISSING_FIELD', 'field': 'supplier_name'},
    }, None)
    assert store.repo.call.call_args.kwargs['p_output']['hold']['code'] == code


@pytest.mark.parametrize('field', ['supplier_name', 'unit_price'])
def test_missing_field_survives_storage_without_changing_public_hold(field):
    store, run_id = graph_store('INFO_EXTRACTOR')
    store.finish_agent_run(run_id, 'COMPLETED', {
        'next_action': 'HOLD', 'reason': '확인이 필요해요.',
        'hold': {'code': 'MISSING_FIELD', 'field': field},
    }, None)
    stored = store.repo.call.call_args.kwargs['p_output']['hold']
    assert stored['field'] == field
    assert from_agent_output({'hold': stored}).model_dump() == {
        'code': 'MISSING_FIELD', 'message': stored['message'],
    }
    assert ('업체명' if field == 'supplier_name' else '단가') in stored['message']


def test_candidate_hold_validates_before_display_copy_and_preserves_web_observations():
    store, run_id = graph_store('CANDIDATE')
    output = CandidateOut(
        next_action='HOLD', reason='후보를 찾지 못했어요.', hold={'code': 'NO_SUITABLE_CANDIDATES'},
        web_observations=[{'status': 'ERROR', 'code': 'SEARCH_BLOCKED'}],
    ).model_dump(mode='json')
    store.finish_agent_run(run_id, 'COMPLETED', output, None)

    name, = store.repo.call.call_args.args
    assert name == 'finish_case_graph_candidate_run'
    sent = store.repo.call.call_args.kwargs
    assert sent['p_output']['hold']['code'] == 'NO_SUITABLE_CANDIDATES'
    assert sent['p_output']['hold']['message']
    assert sent['p_candidate_set']['web_observations'] == output['web_observations']
    assert output['hold'] == {'code': 'NO_SUITABLE_CANDIDATES'}
