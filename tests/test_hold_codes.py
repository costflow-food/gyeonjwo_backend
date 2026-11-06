"""보류 사유 코드 전달 (2026-11-05 sesackt 테스트 후속): ①② HOLD 출력의 hold {code, field} (KAN-146), 업체 관련 거절 쪽지,
② 후보 0곳 문장, 등록 품목 연결의 띄어쓰기 기준. LLM 없이 도구·최종 검사만 확인한다."""
import json
from decimal import Decimal

import pytest
from agent_memory_store import MemoryStore
from langchain_core.messages import ToolMessage

from app.agents import tools
from app.agents.agents import check_extraction, _check_candidates
from app.agents.schemas import CandidateOut, ExtractionIn, ExtractionOut, RunContext
from app.agents.tools import EvidenceRef, QuoteField, _registered_item, save_structured_quote, search_summary
from app.features.work_cases.domain.hold import from_agent_output

CASE = '22222222-2222-2222-2222-222222222222'
MAIL = 'mail-donghae'
BODY = ('박 대리님, 안녕하세요. 동해프레시 한도윤입니다.\n'
        ' - 냉동딸기(IQF) 특품, 주문 단위 10kg 박스\n'
        ' - 기존 50,000원/박스 → 변경 55,000원/박스\n'
        '한도윤 드림\n동해프레시 영업부 | 033-555-0270')


def ctx():
    return RunContext(CASE, 1, 'run-1', 'batch-1')


def field(name, raw):
    return QuoteField(field_name=name, raw_value=raw, evidence=EvidenceRef(mail_id=MAIL, part='body'))


FIELDS = [field('supplier_name', '동해프레시'), field('item_name', '냉동딸기(IQF)'),
          field('unit_price', '55,000원'), field('unit', '박스')]


@pytest.fixture
def store():
    getattr(tools, "_RUN_SUPPLIER_ISSUES", {}).clear()
    memory = MemoryStore()
    memory.add_case(CASE)
    memory.add_mail(MAIL, CASE, BODY, supplier_id=None)      # 발신 주소로 업체가 연결되지 않은 메일
    tools.use_case_store(memory)
    tools.use_quote_store(memory)
    yield memory
    tools.use_case_store(None)
    tools.use_quote_store(None)


def tool_message(name, result):
    return ToolMessage(content=json.dumps(result, ensure_ascii=False), name=name, tool_call_id=f'call-{name}')


# ── 등록 품목 연결: 띄어쓰기만 무시 ──

def test_registered_item_ignores_spaces_only():
    items = [{'id': 'si-1', 'item_name': '냉동딸기 (IQF)', 'specification': '특품 10kg 박스'}]
    assert _registered_item(items, {'item_name': {'value': '냉동딸기(IQF)'}})['id'] == 'si-1'
    assert _registered_item(items, {'item_name': {'value': '냉동딸기(iqf)'}})['id'] == 'si-1'
    # 이름이 포함되는 정도로는 연결하지 않는다 (DB가 다른 품목으로 보고 저장을 거부한다)
    assert _registered_item(items, {'item_name': {'value': '냉동딸기'}}) is None


# ── ① 업체 관련 거절 쪽지 ──

def test_unregistered_supplier_records_issue(store):
    result = save_structured_quote(store, ctx(), FIELDS, [], [], [])
    assert result['status'] == 'REJECTED'
    assert result['rejected'][0]['code'] == 'SUPPLIER_NOT_MATCHED'
    [issue] = store.extraction_issues
    assert issue['code'] == 'SUPPLIER_NOT_MATCHED' and issue['field'] == 'supplier_name'
    assert issue['supplier_name'] == '동해프레시' and issue['mail_ids'] == [MAIL]   # dev_1.0: 메일 ID로 기록


def test_registered_supplier_stores_without_issue(store):
    store.add_supplier('sup-donghae', '동해프레시', 'sales@donghae.example')
    result = save_structured_quote(store, ctx(), FIELDS, [], [], [])
    assert result['status'] == 'STORED'
    assert store.extraction_issues == []


def test_issue_recording_failure_propagates_in_dev(store):
    # dev_1.0(BE 통합 7ce9a2a): 기록 실패는 도구 밖으로 나간다(PR 51 원래 설계는 삼키고 거절 결과 유지). BE 확인 대상
    def refuse(*args):
        raise RuntimeError('SUPPLIER_MATCH_AVAILABLE')
    store.record_extraction_issue = refuse
    with pytest.raises(RuntimeError):
        save_structured_quote(store, ctx(), FIELDS, [], [], [])


def test_not_matched_next_says_hold_for_bare_name(store):
    result = save_structured_quote(store, ctx(), FIELDS, [], [], [])
    assert 'HOLD' in result['next'] and '다시 보내지 말고' in result['next']


def test_not_matched_next_says_retry_for_legal_suffix(store):
    body = BODY + '\n동해프레시 주식회사 대표이사'
    store.mail_messages[MAIL]['body_text'] = body
    fields = [field('supplier_name', '동해프레시 주식회사')] + FIELDS[1:]
    result = save_structured_quote(store, ctx(), fields, [], [], [])
    assert result['rejected'][0]['code'] == 'SUPPLIER_NOT_MATCHED'
    assert '빼고' in result['next'] and '한 번 더' in result['next']


def test_dropping_supplier_name_does_not_overwrite_supplier_issue(store):
    save_structured_quote(store, ctx(), FIELDS, [], [], [])                 # SUPPLIER_NOT_MATCHED 쪽지
    result = save_structured_quote(store, ctx(), FIELDS[1:], [], [], [])    # 업체명을 빼고 다시 보냄
    assert result['rejected'][0]['code'] == 'MISSING_FIELD'
    # dev_1.0: 업체명 누락은 쪽지를 남기지 않는다(미등록·불일치만)
    assert [issue['code'] for issue in store.extraction_issues] == ['SUPPLIER_NOT_MATCHED']


# ── ① 업체가 연결된 메일: 원문 업체명과 대조 (2026-11-06) ──

def link_mail(store, supplier_id):
    store.mail_messages[MAIL]['supplier_id'] = supplier_id


def test_linked_mail_with_other_registered_name_is_mismatch(store):
    store.add_supplier('sup-donghae', '동해프레시', 'sales@donghae.example')
    store.add_supplier('sup-cheongsol', '청솔농산', 'sales@cheongsol.example')
    link_mail(store, 'sup-cheongsol')                 # 잘못 연결된 메일
    result = save_structured_quote(store, ctx(), FIELDS, [], [], [])
    assert result['status'] == 'REJECTED' and result['rejected'][0]['code'] == 'SUPPLIER_MISMATCH'
    [issue] = store.extraction_issues
    assert issue['code'] == 'SUPPLIER_MISMATCH' and issue['supplier_name'] == '동해프레시'


def test_linked_mail_with_same_name_stores(store):
    store.add_supplier('sup-donghae', '동해프레시', 'sales@donghae.example')
    link_mail(store, 'sup-donghae')
    assert save_structured_quote(store, ctx(), FIELDS, [], [], [])['status'] == 'STORED'
    assert store.extraction_issues == []


def test_linked_mail_with_unregistered_name_stores_to_linked_supplier(store):
    store.add_supplier('sup-cheongsol', '청솔농산', 'sales@cheongsol.example')
    link_mail(store, 'sup-cheongsol')                 # 장부에 없는 표기는 대조하지 않고 연결을 따른다
    result = save_structured_quote(store, ctx(), FIELDS, [], [], [])
    assert result['status'] == 'STORED'
    assert store.extraction_issues == []


def test_linked_mail_without_name_stores(store):
    store.add_supplier('sup-donghae', '동해프레시', 'sales@donghae.example')
    link_mail(store, 'sup-donghae')
    assert save_structured_quote(store, ctx(), FIELDS[1:], [], [], [])['status'] == 'STORED'


BAD_SPEC = field('specification', '특품 20kg 박스')       # 원문에 없는 값 → SOURCE_MISMATCH


def test_supplier_mismatch_is_reported_before_other_field_rejections(store):
    store.add_supplier('sup-donghae', '동해프레시', 'sales@donghae.example')
    store.add_supplier('sup-cheongsol', '청솔농산', 'sales@cheongsol.example')
    link_mail(store, 'sup-cheongsol')
    result = save_structured_quote(store, ctx(), FIELDS + [BAD_SPEC], [], [], [])
    assert [r['code'] for r in result['rejected']] == ['SUPPLIER_MISMATCH']
    assert store.extraction_issues[0]['code'] == 'SUPPLIER_MISMATCH'


def test_not_matched_is_reported_before_other_field_rejections(store):
    result = save_structured_quote(store, ctx(), FIELDS + [BAD_SPEC], [], [], [])
    assert [r['code'] for r in result['rejected']] == ['SUPPLIER_NOT_MATCHED']


def test_field_rejection_kept_when_supplier_is_fine(store):
    store.add_supplier('sup-donghae', '동해프레시', 'sales@donghae.example')
    result = save_structured_quote(store, ctx(), FIELDS + [BAD_SPEC], [], [], [])
    assert [r['code'] for r in result['rejected']] == ['SOURCE_MISMATCH']


# ── ① 최종 검사: hold {code, field} ──

def extraction_hold(reason, messages, hold=None):
    data = ExtractionIn(case_id=CASE, input_revision=1, mail_ids=[MAIL])
    output = ExtractionOut(next_action='HOLD', reason=reason, hold=hold)
    return check_extraction('INFO_EXTRACTOR', data, ctx(), output, messages)


def rejected(*items):
    return tool_message('store_structured_mail', {'status': 'REJECTED', 'rejected': list(items)})


def test_earlier_supplier_code_wins_over_later_missing_name():
    messages = [rejected({'field': 'supplier_name', 'code': 'SUPPLIER_NOT_MATCHED'}),
                rejected({'field': 'supplier_name', 'code': 'MISSING_FIELD'})]
    out = extraction_hold('단위를 원문에서 확인할 수 없어 견적을 저장하지 못했습니다.', messages,
                          {'code': 'MISSING_FIELD', 'field': 'unit'})
    assert out.hold.model_dump() == {'code': 'SUPPLIER_NOT_MATCHED', 'field': None}


def test_tool_supplier_code_is_passed_through_even_if_llm_omits_hold():
    messages = [rejected({'field': 'supplier_name', 'code': 'SUPPLIER_NOT_MATCHED'})]
    out = extraction_hold('동해프레시는 거래처 목록에 없는 업체입니다.', messages)
    assert out.hold.code == 'SUPPLIER_NOT_MATCHED'
    assert out.reason == '동해프레시는 거래처 목록에 없는 업체입니다.'
    assert from_agent_output(out.model_dump(mode='json')).code == 'SUPPLIER_NOT_MATCHED'


def test_supplier_mismatch_from_tool():
    messages = [rejected({'field': 'supplier_name', 'code': 'SUPPLIER_MISMATCH'})]
    out = extraction_hold('메일에 연결된 거래처와 원문의 업체명이 달라 견적을 저장하지 않았습니다.', messages,
                          {'code': 'NO_QUOTE_DATA'})
    assert out.hold.code == 'SUPPLIER_MISMATCH'


def test_missing_supplier_name_from_tool_sets_field():
    messages = [rejected({'field': 'supplier_name', 'code': 'MISSING_FIELD'})]
    out = extraction_hold('메일에서 업체명을 찾지 못해 견적을 저장하지 않았습니다.', messages)
    assert out.hold.model_dump() == {'code': 'MISSING_FIELD', 'field': 'supplier_name'}
    assert from_agent_output(out.model_dump(mode='json')).message == '업체명이 확인되지 않아 견적을 저장하지 못했어요.'


def test_invented_name_after_missing_name_stays_missing_field():
    # 2026-11-06 재현: 업체명 누락 → LLM이 발신 주소로 지은 이름 → SOURCE_MISMATCH. 원인은 업체명 누락이다
    messages = [rejected({'field': 'supplier_name', 'code': 'MISSING_FIELD'}),
                rejected({'field': 'supplier_name', 'code': 'SOURCE_MISMATCH'})]
    out = extraction_hold('견적 값의 원문 근거를 확인할 수 없어 저장하지 못했습니다.', messages,
                          {'code': 'MISSING_EVIDENCE'})
    assert out.hold.model_dump() == {'code': 'MISSING_FIELD', 'field': 'supplier_name'}


def test_other_field_evidence_rejection_is_not_missing_name():
    messages = [rejected({'field': 'supplier_name', 'code': 'MISSING_FIELD'}),
                rejected({'field': 'unit_price', 'code': 'SOURCE_MISMATCH'})]
    out = extraction_hold('견적 값의 원문 근거를 확인할 수 없어 저장하지 못했습니다.', messages,
                          {'code': 'MISSING_EVIDENCE'})
    assert out.hold.code == 'MISSING_EVIDENCE'


def test_llm_supplier_code_without_tool_result_is_dropped():
    out = extraction_hold('메일에 연결된 거래처와 원문의 업체명이 달라 견적을 저장하지 않았습니다.', [],
                          {'code': 'SUPPLIER_MISMATCH'})
    assert out.hold is None


@pytest.mark.parametrize('hold, reason', [
    ({'code': 'NO_QUOTE_DATA'}, '이 메일에는 업무 품목의 견적 정보가 없어 저장하지 않았습니다.'),
    ({'code': 'SOURCE_READ_FAILED'}, '첨부를 읽을 수 없어 견적을 저장하지 못했습니다.'),
    ({'code': 'MISSING_FIELD', 'field': 'unit_price'}, '단가를 원문에서 확인할 수 없어 견적을 저장하지 못했습니다.'),
    ({'code': 'MISSING_EVIDENCE'}, '견적 값의 원문 근거를 확인할 수 없어 저장하지 못했습니다.'),
])
def test_llm_judgement_codes_are_kept(hold, reason):
    out = extraction_hold(reason, [], hold)
    assert out.hold.code == hold['code'] and out.hold.field == hold.get('field')
    assert out.reason == reason
    assert from_agent_output(out.model_dump(mode='json')).code == hold['code']


def test_missing_field_message_follows_field():
    out = extraction_hold('단가를 원문에서 확인할 수 없어 견적을 저장하지 못했습니다.', [],
                          {'code': 'MISSING_FIELD', 'field': 'unit_price'})
    assert from_agent_output(out.model_dump(mode='json')).message == '견적 단가가 확인되지 않아 검토를 보류했어요.'


def test_code_prefix_is_removed_from_reason():
    out = extraction_hold('[NO_QUOTE_DATA] 이 메일에는 업무 품목의 견적 정보가 없어 저장하지 않았습니다.', [],
                          {'code': 'NO_QUOTE_DATA'})
    assert out.reason == '이 메일에는 업무 품목의 견적 정보가 없어 저장하지 않았습니다.'


@pytest.mark.parametrize('status', ['REVISION_CHANGED', 'CASE_NOT_FOUND'])
def test_case_changed_hold_is_null(status):
    messages = [tool_message('get_case_context', {'status': status})]
    out = extraction_hold('업무 정보가 바뀌어 이번 추출을 멈췄습니다.', messages, {'code': 'NO_QUOTE_DATA'})
    assert out.hold is None


def test_unknown_cause_hold_is_null():
    out = extraction_hold('저장하지 못했습니다.', [])
    assert out.hold is None
    assert from_agent_output(out.model_dump(mode='json')).code != 'SUPPLIER_NOT_MATCHED'


def test_stored_clears_hold_and_schema_rejects_mismatch():
    with pytest.raises(ValueError):
        ExtractionOut(next_action='STORED', reason='저장했습니다.', quote_version_ids=['q-1'],
                      hold={'code': 'NO_QUOTE_DATA'})
    with pytest.raises(ValueError):
        ExtractionOut(next_action='HOLD', reason='x', hold={'code': 'NO_QUOTE_DATA', 'field': 'unit'})
    with pytest.raises(ValueError):
        ExtractionOut(next_action='HOLD', reason='x', hold={'code': 'HOLD_REASON_UNAVAILABLE'})


# ── ① 저장 뒤 HOLD는 STORED로 (X-42) ──

def stored(quote_version_id):
    return tool_message('store_structured_mail', {'status': 'STORED', 'quote_version_id': quote_version_id})


def extraction_out(messages, ids=(), hold=None):
    data = ExtractionIn(case_id=CASE, input_revision=1, mail_ids=[MAIL])
    output = ExtractionOut(next_action='HOLD', reason='공급 물량을 확인할 수 없어 견적을 저장하지 않았습니다.',
                           quote_version_ids=list(ids), hold=hold)
    return check_extraction('INFO_EXTRACTOR', data, ctx(), output, messages)


def test_hold_without_ids_after_store_becomes_stored():
    out = extraction_out([stored('qv-1')], hold={'code': 'MISSING_FIELD', 'field': 'quantity'})
    assert out.next_action == 'STORED' and out.quote_version_ids == ['qv-1'] and out.hold is None
    assert out.reason == '견적을 저장했습니다. 확인이 필요한 항목은 검토 화면에서 확인해 주세요.'


def test_hold_with_stored_ids_becomes_stored_in_store_order():
    messages = [stored('qv-2'), stored('qv-1'), stored('qv-2')]
    out = extraction_out(messages, ids=['qv-1'], hold={'code': 'NO_QUOTE_DATA'})
    assert out.next_action == 'STORED' and out.quote_version_ids == ['qv-2', 'qv-1'] and out.hold is None


def test_hold_without_store_keeps_hold():
    out = extraction_out([rejected({'field': 'available_quantity', 'code': 'SUPPLY_QUANTITY_MISSING'})])
    assert out.next_action == 'HOLD' and out.quote_version_ids == []
    assert out.hold.model_dump() == {'code': 'MISSING_FIELD', 'field': 'quantity'}


# ── ① 규격 수량은 공급 물량이 아니다 (S3-15) ──

V1_BODY = ('박 대리님, 안녕하세요. 한결냉동식품 영업팀 최서윤입니다.\n'
           ' - 냉동딸기(IQF) 특품 10kg/박스: 5,000원/kg → 5,500원/kg\n'
           ' - 적용일: 2026년 11월 1일 납품분부터\n'
           '최서윤 드림\n한결냉동식품 영업팀 | 031-555-0142')
V1_FIELDS = [field('item_name', '냉동딸기(IQF)'), field('specification', '특품 10kg/박스'),
             field('unit_price', '5,500원'), field('unit', 'kg')]


def v1_store(store, body, message_role='INITIAL'):
    store.add_supplier('sup-hangyeol', '한결냉동식품', 'sales@hangyeol.example')
    store.add_mail(MAIL, CASE, body, supplier_id='sup-hangyeol', message_role=message_role)


def test_spec_quantity_is_not_supply_quantity(store):
    v1_store(store, V1_BODY)
    result = save_structured_quote(store, ctx(), V1_FIELDS + [field('available_quantity', '10kg')], [], [], [])
    assert result['status'] == 'REJECTED'
    assert result['rejected'] == [{'field': 'available_quantity', 'code': 'SUPPLY_QUANTITY_MISSING'}]
    assert store.work_cases[CASE]['quantity'] is None


def test_supply_plan_quantity_sets_case_quantity(store):
    v1_store(store, V1_BODY + '\n - 11월 공급 계획 1,000kg')
    result = save_structured_quote(store, ctx(), V1_FIELDS + [field('available_quantity', '1,000kg')], [], [], [])
    assert result['status'] == 'STORED'
    assert Decimal(store.work_cases[CASE]['quantity']) == 1000 and store.work_cases[CASE]['unit'] == 'kg'


def test_reply_supply_quantity_is_not_checked(store):
    body = '냉동딸기(IQF) 특품 10kg/박스 5,300원/kg\n공급 가능 수량 600kg'
    v1_store(store, body, message_role='QUOTE_REPLY')
    fields = V1_FIELDS[:2] + [field('unit_price', '5,300원'), field('unit', 'kg'),
                              field('available_quantity', '600kg')]
    result = save_structured_quote(store, ctx(), fields, [], [], [])
    assert result['status'] == 'STORED'
    assert store.work_cases[CASE]['quantity'] is None       # 대안 업체 회신의 공급 가능량으로는 채우지 않는다


# ── ② 보류 코드와 후보 0곳 문장 ──

def test_no_registered_handler_sentence_is_not_read_as_supplier_mismatch():
    text = search_summary({'scope': {'searched': 0}, 'excluded': [], 'candidates': []}, '냉동딸기')
    assert '등록 업체가 없어' not in text
    assert from_agent_output({'next_action': 'HOLD', 'reason': text}).code == 'NO_SUITABLE_CANDIDATES'


def test_candidate_hold_gets_code_from_search_result():
    summary = '냉동딸기 취급 업체를 찾지 못해 견적을 요청할 후보가 없습니다.'
    messages = [tool_message('search_supplier_evidence', {'status': 'OK', 'candidates': [], 'summary': summary})]
    out = _check_candidates(CandidateOut(next_action='HOLD', reason=summary), messages)
    assert out.hold.code == 'NO_SUITABLE_CANDIDATES'
    assert out.reason == summary


def test_candidate_hold_conditions_missing_overrides_llm():
    summary = '요청 품목이 아직 확정되지 않아 견적을 요청할 후보 업체를 찾을 수 없습니다.'
    messages = [tool_message('search_supplier_evidence', {'status': 'CONDITIONS_MISSING', 'summary': summary})]
    out = _check_candidates(CandidateOut(next_action='HOLD', reason=summary,
                                         hold={'code': 'NO_SUITABLE_CANDIDATES'}), messages)
    assert out.hold.code == 'CONDITIONS_MISSING'
    assert from_agent_output(out.model_dump(mode='json')).code == 'CONDITIONS_MISSING'


def test_candidate_hold_case_changed_is_null():
    messages = [tool_message('get_case_context', {'status': 'REVISION_CHANGED'})]
    out = _check_candidates(CandidateOut(next_action='HOLD', reason='업무 정보가 바뀌어 멈췄습니다.'), messages)
    assert out.hold is None
