from uuid import UUID

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage

from app.agents import tools
from app.agents.agents import AgentRunner
from app.agents.schemas import AgentRole, ExtractionIn, ExtractionOut, RunContext
from app.agents.tools import ItemNote, QuoteField, save_structured_quote, store_structured_mail
from app.features.review.domain.schemas import FIELD_NAMES

CASE = '6f1c2a54-2f0e-4c1b-9a51-7d1e2b3c4d5e'
OTHER_CASE = '0b9e8d7c-6a5b-4c3d-8e2f-1a0b9c8d7e6f'
MAIL = '공급사: 가나농산\n품목: 냉동 딸기\n변경 단가: 5,450원/kg\n적용일: 2026년 11월 1일\n비고: 11월 물량 일부 지연 가능'


class MemoryQuoteStore:
    def __init__(self, revision=3):
        self.revisions = {CASE: revision, OTHER_CASE: 0}
        self.evidence = {(CASE, 'm1'): MAIL, (OTHER_CASE, 'x1'): '공급사: 다른업체'}
        self.saved = []

    def current_revision(self, case_id):
        return self.revisions.get(case_id)

    def evidence_text(self, case_id, evidence_id):
        return self.evidence.get((case_id, evidence_id))

    def save_quote_version(self, case_id, input_revision, fields, item_notes, unknown_fields):
        self.saved.append({'case_id': case_id, 'input_revision': input_revision, 'fields': fields,
                           'item_notes': item_notes, 'unknown_fields': unknown_fields})
        return f'q{len(self.saved)}'


@pytest.fixture
def store():
    memory = MemoryQuoteStore()
    tools.use_quote_store(memory)
    yield memory
    tools.use_quote_store(None)


def field(name, raw, evidence='m1'):
    return {'field_name': name, 'raw_value': raw, 'evidence_id': evidence, 'location': '본문'}


def run(store, fields, revision=3, case_id=CASE, item_notes=()):
    return save_structured_quote(store, RunContext(case_id, revision), [QuoteField(**f) for f in fields],
                                 [ItemNote(**n) for n in item_notes])


def test_stores_source_values_and_reports_unknown_fields(store):
    result = run(store, [field('supplier_name', '가나농산'), field('new_unit_price', '5,450원/kg'),
                         field('effective_date', '2026년 11월 1일'), field('transport_amount', None, None)],
                 item_notes=[{'text': '11월 물량 일부 지연 가능', 'evidence_id': 'm1'}])
    assert result['status'] == 'STORED' and result['quote_version_id'] == 'q1'
    saved = store.saved[0]
    assert saved['case_id'] == CASE and saved['input_revision'] == 3
    assert {f['field_name']: f['value'] for f in saved['fields']} == {
        'supplier_name': '가나농산', 'new_unit_price': '5450', 'effective_date': '2026-11-01'}
    assert saved['fields'][1]['raw_value'] == '5,450원/kg'
    assert saved['item_notes'] == [{'text': '11월 물량 일부 지연 가능', 'evidence_id': 'm1', 'location': None}]
    assert 'transport_amount' in result['unknown_fields'] and 'item_name' in result['unknown_fields']
    assert set(result['unknown_fields']) == set(FIELD_NAMES) - {'supplier_name', 'new_unit_price', 'effective_date'}


@pytest.mark.parametrize('revision, case_id, status', [(2, CASE, 'REVISION_CHANGED'), (3, 'nope', 'CASE_NOT_FOUND')])
def test_does_not_store_for_stale_or_unknown_case(store, revision, case_id, status):
    assert run(store, [field('supplier_name', '가나농산')], revision, case_id) == {'status': status}
    assert store.saved == []


@pytest.mark.parametrize('bad, code', [
    (field('item_name', '생딸기'), 'SOURCE_MISMATCH'),
    (field('item_name', '공급사: 다른업체', 'x1'), 'EVIDENCE_NOT_IN_CASE'),
    (field('item_name', '냉동 딸기', None), 'MISSING_EVIDENCE'),
    (field('effective_date', '11월 1일'), 'INVALID_DATE'),
    (field('discount', '5,450원/kg'), 'UNKNOWN_FIELD'),
    (field('supplier_name', '가나농산'), 'DUPLICATE_FIELD'),
])
def test_rejects_whole_call_without_storing(store, bad, code):
    result = run(store, [field('supplier_name', '가나농산'), bad])
    assert result == {'status': 'REJECTED', 'rejected': [{'field': bad['field_name'], 'code': code}]}
    assert store.saved == []


def test_rejects_value_that_is_not_a_number(store):
    store.evidence[(CASE, 'm2')] = '변경 단가: 협의 후 안내'
    result = run(store, [field('supplier_name', '가나농산'), field('new_unit_price', '협의 후 안내', 'm2')])
    assert result['rejected'] == [{'field': 'new_unit_price', 'code': 'INVALID_NUMBER'}]


def test_supplier_name_is_required(store):
    result = run(store, [field('new_unit_price', '5,450원/kg')])
    assert result == {'status': 'REJECTED', 'rejected': [{'field': 'supplier_name', 'code': 'MISSING_FIELD'}]}


def test_item_note_must_be_in_source(store):
    result = run(store, [field('supplier_name', '가나농산')], item_notes=[{'text': '전량 공급 보장', 'evidence_id': 'm1'}])
    assert result['rejected'] == [{'field': 'item_notes[0]', 'code': 'SOURCE_MISMATCH'}]


def test_llm_cannot_pass_case_or_revision():
    visible = store_structured_mail.tool_call_schema.model_json_schema()['properties']
    assert set(visible) == {'fields', 'item_notes'}


def test_docstring_lists_every_field_name():
    assert ', '.join(FIELD_NAMES) in ' '.join(store_structured_mail.description.split())


class FakeModel(GenericFakeChatModel):
    def bind_tools(self, tools, **kwargs):
        return self


def call(name, args, index):
    return AIMessage(content='', tool_calls=[{'name': name, 'args': args, 'id': f'c{index}'}])


def extract(model, case_id=CASE):
    return AgentRunner(model)(AgentRole.INFO_EXTRACTOR, ExtractionIn(case_id=UUID(case_id), input_revision=3,
                                                                    mail_ids=['m1']))


def test_extractor_stores_under_running_case_then_returns_quote_version_id(store):
    model = FakeModel(messages=iter([
        call('store_structured_mail', {'fields': [field('supplier_name', '가나농산')]}, 1),
        call('ExtractionOut', {'next_action': 'STORED', 'reason': '견적 저장', 'quote_version_ids': ['q1']}, 2)]))
    out = extract(model)
    assert isinstance(out, ExtractionOut) and out.quote_version_ids == ['q1']
    assert [(s['case_id'], s['input_revision']) for s in store.saved] == [(CASE, 3)]


def test_case_id_written_by_llm_is_ignored(store):
    model = FakeModel(messages=iter([
        call('store_structured_mail', {'case_id': OTHER_CASE, 'expected_revision': 0,
                                       'fields': [field('supplier_name', '가나농산')]}, 1),
        call('ExtractionOut', {'next_action': 'STORED', 'reason': '견적 저장', 'quote_version_ids': ['q1']}, 2)]))
    extract(model)
    assert [s['case_id'] for s in store.saved] == [CASE]


def test_unconfigured_store_fails_the_agent_call():
    model = FakeModel(messages=iter([
        call('store_structured_mail', {'fields': [field('supplier_name', '가나농산')]}, 1)]))
    with pytest.raises(RuntimeError, match='not configured'):
        extract(model)
