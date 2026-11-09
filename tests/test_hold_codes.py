"""보류 사유 코드 전달 (2026-11-05 sesackt 테스트 후속): ①② HOLD 출력의 hold {code, field} (KAN-146), 업체 관련 거절 쪽지,
② 후보 0곳 문장, 등록 품목 연결의 띄어쓰기 기준. LLM 없이 도구·최종 검사만 확인한다."""
import json

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
        ' - 월 공급 물량 100박스\n'
        '한도윤 드림\n동해프레시 영업부 | 033-555-0270')


def ctx():
    return RunContext(CASE, 1, 'run-1', 'batch-1')


def field(name, raw):
    return QuoteField(field_name=name, raw_value=raw, evidence=EvidenceRef(mail_id=MAIL, part='body'))


FIELDS = [field('supplier_name', '동해프레시'), field('item_name', '냉동딸기(IQF)'),
          field('unit_price', '55,000원'), field('unit', '박스'), field('available_quantity', '100박스')]


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

def test_registered_item_ignores_case_and_repeated_spaces_only():
    items = [{'id': 'si-1', 'item_name': '냉동딸기 (IQF)', 'specification': '특품 10kg 박스'}]
    assert _registered_item(items, {'item_name': {'value': ' 냉동딸기  (iqf) '}}, {})['id'] == 'si-1'
    # 띄어쓰기 유무가 다르면 다른 품목이다 (DB 품목 키 기준과 같다, tools._item_key)
    assert _registered_item(items, {'item_name': {'value': '냉동딸기(IQF)'}}, {}) is None
    # 이름이 포함되는 정도로는 연결하지 않는다 (DB가 다른 품목으로 보고 저장을 거부한다)
    assert _registered_item(items, {'item_name': {'value': '냉동딸기'}}, {}) is None


# ── 등록 품목 연결: 업무에 이미 있는 품목명·품목 키와도 맞아야 한다 (KAN-162, DB 대조 기준) ──

STRAWBERRY = [{'id': 'si-1', 'item_name': '냉동딸기 (IQF)', 'standardized_item_key': 'frozen_strawberry'}]


def test_registered_item_skips_when_case_item_name_differs():
    case = {'item_name': '냉동딸기'}
    assert _registered_item(STRAWBERRY, {'item_name': {'value': '냉동딸기 (IQF)'}}, case) is None


def test_registered_item_skips_when_case_item_key_differs():
    case = {'item_name': '냉동딸기 (IQF)', 'standardized_item_key': 'item_ceedb4a1'}
    assert _registered_item(STRAWBERRY, {'item_name': {'value': '냉동딸기 (IQF)'}}, case) is None


def test_registered_item_links_when_case_item_empty():
    assert _registered_item(STRAWBERRY, {'item_name': {'value': '냉동딸기 (IQF)'}}, {'item_name': None})['id'] == 'si-1'


def test_registered_item_links_when_only_letter_case_differs():
    case = {'item_name': '냉동딸기 (iqf)', 'standardized_item_key': 'frozen_strawberry'}
    assert _registered_item(STRAWBERRY, {'item_name': {'value': '냉동딸기 (IQF)'}}, case)['id'] == 'si-1'


# ── ① 업체 관련 거절 쪽지 ──

def test_unregistered_supplier_records_issue(store):
    result = save_structured_quote(store, ctx(), FIELDS, [], [], [])
    assert result['status'] == 'REJECTED'
    assert result['rejected'][0]['code'] == 'SUPPLIER_NOT_MATCHED'
    [issue] = store.extraction_issues
    assert issue['code'] == 'SUPPLIER_NOT_MATCHED' and issue['field'] == 'supplier_name'
    assert issue['supplier_name'] == '동해프레시' and issue['agent_run_id'] == 'run-1'
    # 업체명이 적힌 원문 줄을 근거로 남긴다
    assert issue['evidence'] == [{'evidence_id': f'{MAIL}:body', 'excerpt': '박 대리님, 안녕하세요. 동해프레시 한도윤입니다.'}]


def test_registered_supplier_stores_without_issue(store):
    store.add_supplier('sup-donghae', '동해프레시', 'sales@donghae.example')
    result = save_structured_quote(store, ctx(), FIELDS, [], [], [])
    assert result['status'] == 'STORED'
    assert store.extraction_issues == []


def test_issue_recording_failure_keeps_rejection(store):
    # 기록이 실패해도 거절과 다음 행동 안내는 그대로 돌려준다 (tools._record_supplier_issue)
    def refuse(*args):
        raise RuntimeError('SUPPLIER_MATCH_AVAILABLE')
    store.record_extraction_issue = refuse
    result = save_structured_quote(store, ctx(), FIELDS, [], [], [])
    assert result['rejected'][0]['code'] == 'SUPPLIER_NOT_MATCHED' and 'HOLD' in result['next']


def test_not_matched_next_says_hold_for_bare_name(store):
    result = save_structured_quote(store, ctx(), FIELDS, [], [], [])
    assert 'HOLD' in result['next'] and '다시 보내지 말고' in result['next']


def test_not_matched_next_says_hold_for_legal_suffix(store):
    # S3-7: 법인 표기는 도구가 빼고 찾는다. 빼도 등록 업체가 없으면 다시 보내지 않고 HOLD
    store.mail_messages[MAIL]['body_text'] = BODY + '\n동해프레시 주식회사 대표이사'
    fields = [field('supplier_name', '동해프레시 주식회사')] + FIELDS[1:]
    result = save_structured_quote(store, ctx(), fields, [], [], [])
    assert result['rejected'][0]['code'] == 'SUPPLIER_NOT_MATCHED'
    assert 'HOLD' in result['next'] and '다시 보내지 말고' in result['next']


def test_not_matched_next_says_hold_for_department(store):
    # KAN-163: 부서·직책도 도구가 빼고 찾는다. 빼도 등록 업체가 없으면 다시 보내지 않고 HOLD
    fields = [field('supplier_name', '동해프레시 영업부')] + FIELDS[1:]
    result = save_structured_quote(store, ctx(), fields, [], [], [])
    assert result['rejected'][0]['code'] == 'SUPPLIER_NOT_MATCHED'
    assert 'HOLD' in result['next'] and '다시 보내지 말고' in result['next']


# ── KAN-163: 부서·직책 표기 앞부분만 남긴 회사명으로 정확 일치 ──

@pytest.mark.parametrize('registered, written', [('동해프레시', '동해프레시 영업부'),
                                                 ('동해프레시', '동해프레시 주식회사 영업팀 홍길동'),
                                                 ('한결냉동식품', '한결냉동식품 영업팀 최서윤')])
def test_department_is_removed_to_match_registered_supplier(store, registered, written):
    store.add_supplier('sup-1', registered, 'sales@supplier.example')
    store.mail_messages[MAIL]['body_text'] = BODY + f'\n{written}'
    fields = [field('supplier_name', written)] + FIELDS[1:]
    result = save_structured_quote(store, ctx(), fields, [], [], [])
    assert result['status'] == 'STORED' and store.extraction_issues == []
    [record] = store.quote_versions.values()
    assert record.supplier_id == 'sup-1'
    saved = record.mail_normalized[MAIL]['fields']['supplier_name']
    assert saved['value'] == registered and saved['raw_value'] == written


def test_department_removal_does_not_pick_one_of_duplicates(store):
    store.add_supplier('sup-a', '동해프레시', 'a@donghae.example')
    store.add_supplier('sup-b', '동해프레시', 'b@donghae.example')                   # 부서명만 다른 동명 업체
    fields = [field('supplier_name', '동해프레시 영업부')] + FIELDS[1:]
    result = save_structured_quote(store, ctx(), fields, [], [], [])
    assert result['rejected'][0]['code'] == 'SUPPLIER_NOT_MATCHED'
    assert store.quote_versions == {}


# ── S3-7: 법인 표기 정규화 (등록 업체와 정확 일치만, 저장 값은 등록 이름) ──

@pytest.mark.parametrize('written', ['동해프레시 주식회사', '주식회사 동해프레시', '(주)동해프레시', '( 주 ) 동해프레시',
                                     '㈜동해프레시', '동해프레시(주)', '유한책임회사 동해프레시', '합자회사 동해프레시'])
def test_legal_form_is_removed_to_match_registered_supplier(store, written):
    store.add_supplier('sup-donghae', '동해프레시', 'sales@donghae.example')
    store.mail_messages[MAIL]['body_text'] = BODY + f'\n{written}'
    fields = [field('supplier_name', written)] + FIELDS[1:]
    result = save_structured_quote(store, ctx(), fields, [], [], [])
    assert result['status'] == 'STORED' and store.extraction_issues == []
    [record] = store.quote_versions.values()
    assert record.supplier_id == 'sup-donghae'
    saved = record.mail_normalized[MAIL]['fields']['supplier_name']
    # DB(store_case_graph_quote)는 연결 없는 메일의 업체명 값이 등록 이름과 같아야 받는다. 원문 표기는 raw_value에 남긴다
    assert saved['value'] == '동해프레시' and saved['raw_value'] == written


def test_exact_registered_name_with_legal_form_is_kept(store):
    store.add_supplier('sup-donghae', '동해프레시 주식회사', 'sales@donghae.example')   # 법인 표기째 등록된 업체
    store.mail_messages[MAIL]['body_text'] = BODY + '\n동해프레시 주식회사'
    fields = [field('supplier_name', '동해프레시 주식회사')] + FIELDS[1:]
    assert save_structured_quote(store, ctx(), fields, [], [], [])['status'] == 'STORED'
    [record] = store.quote_versions.values()
    assert record.mail_normalized[MAIL]['fields']['supplier_name']['value'] == '동해프레시 주식회사'


def test_legal_form_removal_does_not_match_partial_name(store):
    store.add_supplier('sup-donghae', '동해프레시푸드', 'sales@donghae.example')      # 이름이 포함될 뿐인 업체
    store.mail_messages[MAIL]['body_text'] = BODY + '\n(주)동해프레시'
    fields = [field('supplier_name', '(주)동해프레시')] + FIELDS[1:]
    result = save_structured_quote(store, ctx(), fields, [], [], [])
    assert result['rejected'][0]['code'] == 'SUPPLIER_NOT_MATCHED'


def test_legal_form_removal_does_not_pick_one_of_duplicates(store):
    store.add_supplier('sup-a', '동해프레시', 'a@donghae.example')
    store.add_supplier('sup-b', '동해프레시', 'b@donghae.example')                   # 같은 이름이 둘이면 연결하지 않는다
    store.mail_messages[MAIL]['body_text'] = BODY + '\n동해프레시 주식회사'
    fields = [field('supplier_name', '동해프레시 주식회사')] + FIELDS[1:]
    result = save_structured_quote(store, ctx(), fields, [], [], [])
    assert result['rejected'][0]['code'] == 'SUPPLIER_NOT_MATCHED'


def test_linked_mail_with_legal_form_of_other_supplier_is_mismatch(store):
    store.add_supplier('sup-donghae', '동해프레시', 'sales@donghae.example')
    store.add_supplier('sup-cheongsol', '청솔농산', 'sales@cheongsol.example')
    link_mail(store, 'sup-donghae')
    store.mail_messages[MAIL]['body_text'] = BODY + '\n(주)청솔농산'
    fields = [field('supplier_name', '(주)청솔농산')] + FIELDS[1:]
    result = save_structured_quote(store, ctx(), fields, [], [], [])
    assert result['rejected'][0]['code'] == 'SUPPLIER_MISMATCH'


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
