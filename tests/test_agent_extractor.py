from uuid import UUID

import pytest
from agent_memory_store import MemoryStore
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage

from app.agents import tools
from app.agents.agents import AgentRunner
from app.agents.schemas import AgentRole, ExtractionIn, ExtractionOut, RunContext
from app.agents.tools import (CostTerm, ItemNote, PriceTier, QuoteField, save_structured_quote,
                              store_structured_mail)

CASE = '6f1c2a54-2f0e-4c1b-9a51-7d1e2b3c4d5e'
OTHER_CASE = '0b9e8d7c-6a5b-4c3d-8e2f-1a0b9c8d7e6f'
MAIL = ('가나농산입니다. 냉동 딸기(특품) 10kg/박스 단가를 기존 52,000원에서 54,500원으로 조정합니다.\n'
        '통화: 원\n적용일: 2026년 12월 1일\n사유: 원물 시세 상승\n운송비 80,000원(1회)은 별도이며 귀사 부담입니다.\n'
        '비고: 12월 물량 중 20%는 입고 지연 가능')


@pytest.fixture
def store():
    memory = MemoryStore()
    memory.add_case(CASE)
    memory.add_case(OTHER_CASE)
    memory.add_supplier('sup-ganna', '가나농산')
    memory.add_supplier('sup-daon', '다온푸드')
    memory.add_mail('mail-1', CASE, MAIL, supplier_id='sup-ganna')
    memory.add_mail('mail-x', OTHER_CASE, '가나농산 냉동 딸기 1원', supplier_id='sup-ganna')
    tools.use_quote_store(memory)
    yield memory
    tools.use_quote_store(None)


def ev(mail='mail-1', part='body', page=None):
    return {'mail_id': mail, 'part': part, 'page': page}


def f(name, raw, mail='mail-1', part='body', page=None):
    return {'field_name': name, 'raw_value': raw, 'evidence': ev(mail, part, page) if raw is not None else None}


BASE = [f('supplier_name', '가나농산'), f('item_name', '냉동 딸기'), f('unit_price', '54,500원'), f('unit', '박스')]


def run(store, fields, tiers=(), costs=(), notes=(), revision=1, case_id=CASE):
    return save_structured_quote(store, RunContext(case_id, revision), [QuoteField(**x) for x in fields],
                                 [PriceTier(**x) for x in tiers], [CostTerm(**x) for x in costs],
                                 [ItemNote(**x) for x in notes])


def test_first_quote_is_stored_in_06_quote_versions_shape(store):
    result = run(store, BASE + [
        f('specification', '특품'), f('order_unit', '박스'), f('quantity_per_order_unit', '10kg'),
        f('currency', '원'), f('effective_from', '2026년 12월 1일'), f('valid_until', None),
        f('previous_unit_price', '52,000원'), f('increase_reason', '원물 시세 상승')],
        costs=[{'cost_type': 'SHIPPING', 'source_text': '운송비 80,000원(1회)은 별도이며 귀사 부담', 'amount': '80,000원',
                'basis': '1회', 'payer': '귀사 부담', 'included_in_price': False, 'evidence': ev()}],
        notes=[{'text': '12월 물량 중 20%는 입고 지연 가능', 'evidence': ev()}])
    assert result == {'status': 'STORED', 'quote_version_id': 'qv-1', 'version_no': 1, 'supersedes_id': None,
                      'extraction_status': 'CONFIRMED', 'unknown_fields': ['valid_until'], 'carried_fields': []}
    row = store.quote_versions['qv-1']
    assert (row.work_case_id, row.supplier_id, row.source_mail_id) == (CASE, 'sup-ganna', 'mail-1')
    assert (row.unit_price, row.unit, row.shipping_cost, row.effective_from, row.valid_until) == (
        '54500', 'box', '80000', '2026-12-01', None)
    assert row.pricing_terms_jsonb == {'currency': 'KRW', 'order_unit': 'box', 'quantity_per_order_unit': '10'}
    assert row.checks_jsonb == {'item_name': {'state': 'STATED', 'value': '냉동 딸기'},
                                'specification': {'state': 'STATED', 'value': '특품'}}
    assert row.cost_terms_jsonb[0]['amount'] == '80000' and row.cost_terms_jsonb[0]['payer'] == '귀사 부담'
    assert row.item_notes_jsonb == [{'text': '12월 물량 중 20%는 입고 지연 가능', 'mail_id': 'mail-1',
                                     'location': 'mail-1 본문'}]
    assert row.evidence_jsonb['unit_price'] == {'raw_value': '54,500원', 'value': '54500', 'mail_id': 'mail-1',
                                                'location': 'mail-1 본문'}
    # 인상 전 단가·사유는 견적 버전이 아니라 메일 정규화 값에만 둔다
    assert 'previous_unit_price' not in row.evidence_jsonb
    normalized = store.mail_messages['mail-1']['normalized_jsonb']
    assert normalized['fields']['previous_unit_price']['value'] == '52000'
    assert normalized['fields']['increase_reason']['value'] == '원물 시세 상승'
    # 최초 추출이 업무 품목을 채우고, 저장이 input_revision을 올리지 않는다
    assert store.work_cases[CASE]['item_name'] == '냉동 딸기' and store.work_cases[CASE]['input_revision'] == 1
    assert store.price_history == [{'quote_version_id': 'qv-1', 'unit_price': '54500', 'unit': 'box',
                                    'effective_from': '2026-12-01'}]


def test_reply_becomes_new_version_and_carries_values_not_in_the_reply(store):
    run(store, BASE + [f('effective_from', '2026년 12월 1일')])
    store.add_mail('mail-2', CASE, '가나농산 회신: 운송비는 회당 60,000원입니다. 유효기간 2026년 12월 31일',
                   supplier_id='sup-ganna', message_role='CLARIFICATION_REPLY')
    result = run(store, [f('supplier_name', '가나농산', 'mail-2'), f('valid_until', '2026년 12월 31일', 'mail-2'),
                         f('effective_from', None)],
                 costs=[{'cost_type': 'SHIPPING', 'source_text': '운송비는 회당 60,000원', 'amount': '60,000원',
                         'basis': '회당', 'evidence': ev('mail-2')}])
    assert result['status'] == 'STORED' and result['version_no'] == 2 and result['supersedes_id'] == 'qv-1'
    assert result['unknown_fields'] == []          # null로 보냈지만 이전 버전에서 이어받음
    assert set(result['carried_fields']) == {'item_name', 'unit_price', 'unit', 'effective_from'}
    row = store.quote_versions['qv-2']
    assert row.source_mail_id == 'mail-2' and row.unit_price == '54500' and row.valid_until == '2026-12-31'
    assert row.evidence_jsonb['unit_price']['carried_from'] == 'qv-1'
    assert row.evidence_jsonb['unit_price']['mail_id'] == 'mail-1'
    assert row.cost_terms_jsonb[0]['amount'] == '60000'
    assert row.shipping_cost is None               # 단가 포함·별도가 원문에 없어 열에는 넣지 않음


def test_attachment_page_is_the_evidence(store):
    store.add_mail('mail-3', CASE, '첨부 참고. 가나농산', supplier_id='sup-ganna', attachments=[
        {'attachment_ref': 'att-1', 'filename': 'quote.pdf', 'pages': ['표지', '품목 냉동 딸기 | 단가 5,300원/kg']}])
    result = run(store, [f('supplier_name', '가나농산', 'mail-3'), f('item_name', '냉동 딸기', 'mail-3', 'att-1', 2),
                         f('unit_price', '5,300원/kg', 'mail-3', 'att-1', 2), f('unit', 'kg', 'mail-3', 'att-1', 2)])
    assert result['status'] == 'STORED'
    assert store.quote_versions['qv-1'].evidence_jsonb['unit_price']['location'] == 'mail-3 quote.pdf p.2'


@pytest.mark.parametrize('revision, case_id, status', [(2, CASE, 'REVISION_CHANGED'), (1, 'nope', 'CASE_NOT_FOUND')])
def test_does_not_store_for_stale_or_unknown_case(store, revision, case_id, status):
    assert run(store, BASE, revision=revision, case_id=case_id) == {'status': status}
    assert store.quote_versions == {}


def test_revision_change_during_insert_is_detected(store):
    original = store.insert_quote_version

    def bump_then_insert(record, expected):
        store.work_cases[CASE]['input_revision'] = 2
        return original(record, expected)
    store.insert_quote_version = bump_then_insert
    assert run(store, BASE) == {'status': 'REVISION_CHANGED'}
    assert store.quote_versions == {}


@pytest.mark.parametrize('bad, code', [
    (f('specification', '최상품'), 'SOURCE_MISMATCH'),
    (f('specification', '냉동 딸기', 'mail-x'), 'EVIDENCE_NOT_IN_CASE'),
    ({'field_name': 'specification', 'raw_value': '특품', 'evidence': None}, 'MISSING_EVIDENCE'),
    (f('effective_from', '12월 1일'), 'INVALID_DATE'),
    (f('unit_price_old', '52,000원'), 'UNKNOWN_FIELD'),
    (f('supplier_name', '가나농산'), 'DUPLICATE_FIELD'),
])
def test_rejects_whole_call_without_storing(store, bad, code):
    result = run(store, BASE + [bad])
    assert result == {'status': 'REJECTED', 'rejected': [{'field': bad['field_name'], 'code': code}]}
    assert store.quote_versions == {}


def test_unit_is_required_with_unit_price(store):
    result = run(store, BASE[:3])
    assert result == {'status': 'REJECTED', 'rejected': [{'field': 'unit', 'code': 'MISSING_FIELD'}]}


def test_supplier_must_be_stated_and_registered(store):
    assert run(store, BASE[1:])['rejected'] == [{'field': 'supplier_name', 'code': 'MISSING_FIELD'}]
    store.add_mail('mail-4', CASE, '새벽상회 냉동 딸기 5,000원/kg')
    result = run(store, [f('supplier_name', '새벽상회', 'mail-4'), f('item_name', '냉동 딸기', 'mail-4')])
    assert result == {'status': 'REJECTED', 'rejected': [{'field': 'supplier_name', 'code': 'SUPPLIER_NOT_MATCHED'}]}
    assert store.suppliers.keys() == {'sup-ganna', 'sup-daon'}


def test_item_outside_the_case_is_rejected(store):
    store.work_cases[CASE]['item_name'] = '냉동 블루베리'
    assert run(store, BASE)['rejected'] == [{'field': 'item_name', 'code': 'ITEM_OUT_OF_CASE'}]


def test_foreign_currency_is_stored_for_review(store):
    store.add_mail('mail-5', CASE, 'Daon Food: frozen strawberry USD 3.20/kg', supplier_id='sup-daon')
    result = run(store, [f('supplier_name', 'Daon Food', 'mail-5'), f('unit_price', '3.20', 'mail-5'),
                         f('unit', 'kg', 'mail-5'), f('currency', 'USD', 'mail-5')])
    assert result['extraction_status'] == 'NEEDS_REVIEW'
    assert store.quote_versions['qv-1'].pricing_terms_jsonb['currency'] == 'USD'


def test_free_shipping_needs_free_in_the_source(store):
    store.add_mail('mail-6', CASE, '다온푸드 냉동 딸기 5,200원/kg, 배송비 무료(단가 별도 청구 없음)', supplier_id='sup-daon')
    shipping = {'cost_type': 'SHIPPING', 'source_text': '배송비 무료', 'amount': '무료', 'included_in_price': False,
                'evidence': ev('mail-6')}
    result = run(store, [f('supplier_name', '다온푸드', 'mail-6')], costs=[shipping])
    assert result['status'] == 'STORED' and store.quote_versions['qv-1'].shipping_cost == '0'
    bad = shipping | {'source_text': '5,200원/kg', 'amount': '무료'}
    assert run(store, [f('supplier_name', '다온푸드', 'mail-6')], costs=[bad])['rejected'] == [
        {'field': 'cost_terms[0]', 'code': 'SOURCE_MISMATCH'}]


def test_price_tiers_are_checked_for_overlap(store):
    store.add_mail('mail-7', CASE, '다온푸드 냉동 딸기 0~500kg 5,200원/kg, 300kg 이상 5,000원/kg', supplier_id='sup-daon')
    tiers = [{'min_quantity': '0', 'max_quantity': '500kg', 'unit_price': '5,200원/kg', 'evidence': ev('mail-7')},
             {'min_quantity': '300kg', 'unit_price': '5,000원/kg', 'evidence': ev('mail-7')}]
    assert run(store, [f('supplier_name', '다온푸드', 'mail-7')], tiers=tiers)['rejected'] == [
        {'field': 'price_tiers', 'code': 'TIER_OVERLAP'}]
    tiers[1]['min_quantity'] = None
    assert run(store, [f('supplier_name', '다온푸드', 'mail-7')], tiers=tiers)['status'] == 'STORED'
    assert store.quote_versions['qv-1'].pricing_terms_jsonb['price_tiers'][0]['max_quantity'] == '500'


def test_same_mail_stored_twice_keeps_one_version(store):
    first, second = run(store, BASE), run(store, BASE)
    assert first['quote_version_id'] == second['quote_version_id'] == 'qv-1'
    assert len(store.quote_versions) == 1


def test_llm_cannot_pass_case_or_revision():
    visible = store_structured_mail.tool_call_schema.model_json_schema()['properties']
    assert set(visible) == {'fields', 'price_tiers', 'cost_terms', 'item_notes'}


class FakeModel(GenericFakeChatModel):
    def bind_tools(self, tools, **kwargs):
        return self


def call(name, args, index):
    return AIMessage(content='', tool_calls=[{'name': name, 'args': args, 'id': f'c{index}'}])


def test_extractor_stores_under_running_case_and_records_the_run(store):
    model = FakeModel(messages=iter([
        call('store_structured_mail', {'case_id': OTHER_CASE, 'fields': BASE}, 1),
        call('ExtractionOut', {'next_action': 'STORED', 'reason': '견적 저장', 'quote_version_ids': ['qv-1']}, 2)]))
    runner = AgentRunner(model, runs=store)
    out = runner(AgentRole.INFO_EXTRACTOR, ExtractionIn(case_id=UUID(CASE), input_revision=1, mail_ids=['mail-1']))
    assert isinstance(out, ExtractionOut) and out.quote_version_ids == ['qv-1']
    assert store.quote_versions['qv-1'].work_case_id == CASE
    run_row = store.agent_runs['run-1']
    assert run_row['status'] == 'COMPLETED' and run_row['agent_role'] == AgentRole.INFO_EXTRACTOR
    assert run_row['output_jsonb']['quote_version_ids'] == ['qv-1']


def test_unconfigured_store_fails_the_agent_call():
    model = FakeModel(messages=iter([call('store_structured_mail', {'fields': BASE}, 1)]))
    with pytest.raises(RuntimeError, match='not configured'):
        AgentRunner(model)(AgentRole.INFO_EXTRACTOR, ExtractionIn(case_id=UUID(CASE), input_revision=1, mail_ids=[]))
