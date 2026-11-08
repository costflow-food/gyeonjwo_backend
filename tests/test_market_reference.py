from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest

from app.features.work_cases.application.market_reference import (
    KST, MarketReferenceService, reference_from_rows, series_for)
from scripts.check_market_scenario import PUBLIC_ROW, ITEM, SPEC, run_scenario

NOW = datetime(2026, 11, 8, tzinfo=KST)


def test_exact_series_and_mass_conversion():
    assert series_for(ITEM, SPEC) == 'garlic_daeseo'
    assert series_for('깐마늘', '국산') is None
    assert series_for('피마늘', SPEC) is None
    assert series_for(ITEM, SPEC + ' 수입') is None
    assert series_for('냉동 딸기', '설향 상품') is None
    assert series_for('냉동 삼겹살', '수입') is None
    assert series_for('다진 깐마늘', SPEC) is None
    assert series_for('냉동 블루베리', '수입 상품 1kg') == 'frozen_blueberry_imported'
    assert series_for('냉동 블루베리', '수입 1kg') is None
    result = reference_from_rows([PUBLIC_ROW], 'garlic_daeseo', NOW)
    assert result['price_won_per_kg'] == '8960.00'
    assert result['age_days'] == 2
    assert result['previous_prices'] == {}


@pytest.mark.parametrize('changes,status', [
    ({'unit': '접'}, 'INVALID_DATA'), ({'exmn_dd_prc': 'NaN'}, 'INVALID_DATA'),
    ({'unit_sz': '0'}, 'INVALID_DATA'), ({'exmn_ymd': '20261109'}, 'INVALID_DATA'),
    ({'exmn_ymd': '20180105'}, 'STALE'), ({'exmn_ymd': '-'}, 'INVALID_DATA'),
    ({'exmn_dd_prc': '1e999'}, 'INVALID_DATA'), ({'unit_sz': '1e-999'}, 'INVALID_DATA'),
    ({'exmn_dd_prc': '0.001', 'unit_sz': '100000'}, 'INVALID_DATA'),
])
def test_bad_or_stale_data_is_never_a_current_price(changes, status):
    assert reference_from_rows([PUBLIC_ROW | changes], 'garlic_daeseo', NOW)['status'] == status


def test_ambiguous_and_missing_rows():
    assert reference_from_rows([PUBLIC_ROW, PUBLIC_ROW], 'garlic_daeseo', NOW)['status'] == 'AMBIGUOUS'
    assert reference_from_rows([], 'garlic_daeseo', NOW)['status'] == 'NOT_FOUND'


def test_pagination_cache_and_incomplete_response():
    calls = []
    def handler(request):
        assert request.url.params['serviceKey'] == 'key+/='
        page = int(request.url.params['pageNo'])
        calls.append(page)
        rows = [{'item_cd': str(i)} for i in range(100)] if page == 1 else [PUBLIC_ROW]
        return httpx.Response(200, json={'response': {'header': {'resultCode': '0'},
            'body': {'totalCount': 101, 'items': {'item': rows}}}})
    service = MarketReferenceService('key%2B%2F%3D', transport=httpx.MockTransport(handler))
    assert len(service._load()[0]) == 101
    assert len(service._load()[0]) == 101
    assert calls == [1, 2]
    broken = MarketReferenceService('test-key', transport=httpx.MockTransport(lambda _: httpx.Response(200,
        json={'response': {'header': {'resultCode': '0'}, 'body': {'totalCount': 2, 'items': {'item': []}}}})))
    assert broken._load() == (None, 'SOURCE_UNAVAILABLE')


def test_outage_and_missing_key_do_not_leak_or_fabricate_price():
    def fail(request):
        raise httpx.ConnectError('secret-query-do-not-leak', request=request)
    service = MarketReferenceService('secret-key', transport=httpx.MockTransport(fail))
    result = service.get({'item_name': ITEM, 'specification': SPEC})
    assert result['status'] == 'SOURCE_UNAVAILABLE'
    assert 'secret' not in str(result)
    assert MarketReferenceService().get({'item_name': ITEM, 'specification': SPEC})['status'] == 'NOT_CONFIGURED'


def test_successful_fetch_does_not_log_query_credential(caplog):
    import logging
    caplog.set_level(logging.DEBUG)
    service = MarketReferenceService('credential-must-not-appear', transport=httpx.MockTransport(
        lambda _: httpx.Response(200, json={'response': {'header': {'resultCode': '0'},
            'body': {'totalCount': 1, 'items': {'item': PUBLIC_ROW}}}})))
    assert service._load()[0] == [PUBLIC_ROW]
    assert 'credential-must-not-appear' not in caplog.text


@pytest.mark.parametrize('payload', [None, [], {'response': None},
    {'response': {'header': {'resultCode': '0'}, 'body': {'totalCount': 1, 'items': ['bad']}}}])
def test_malformed_provider_payload_is_unavailable(payload):
    service = MarketReferenceService('key', transport=httpx.MockTransport(
        lambda _: httpx.Response(200, json=payload)))
    assert service._load() == (None, 'SOURCE_UNAVAILABLE')


def test_route_scopes_case_before_market_lookup():
    from app.core.errors import ServiceError
    from app.features.work_cases.api.review_card import market_reference
    from scripts.check_market_scenario import CASE
    repo = Mock()
    repo.case.side_effect = ServiceError('RESOURCE_NOT_FOUND', '업무를 찾을 수 없습니다.', 404)
    market = Mock()
    with pytest.raises(ServiceError) as error:
        market_reference(CASE, SimpleNamespace(user_id='other-owner'), SimpleNamespace(repo=repo, market=market))
    assert error.value.status == 404
    repo.case.assert_called_once_with('other-owner', CASE)
    market.get.assert_not_called()


def test_end_to_end_graph_tools_and_review_api():
    result = run_scenario()
    assert [r['status'] for r in result['rounds']] == ['WAITING_REPLY', 'WAITING_REPLY', 'COMPLETE']
    roles = {step['role'] for r in result['rounds'] for step in r['steps']}
    assert roles == {'INFO_EXTRACTOR', 'CANDIDATE', 'INFO_PRIORITY', 'RESPONSE_PLAN', 'COUNTER_OFFER'}
    card = result['card']
    assert card['review_outcome'] == 'COMPLETED'
    assert sorted(p['total_amount'] for p in card['plans']) == sorted(['10340000', '9670000', '9700000', '9580000', '9610000'])
    assert len({p['plan_id'] for p in card['plans']}) == 5
    assert 'SELECT_PLAN' in card['allowed_actions']
    assert card['counter_offer']['target']['target_unit_price'] == '9776'
    assert result['draft_purposes'].count('RFQ') == 3
    assert result['draft_purposes'].count('CLARIFICATION') == 1
    assert result['draft_purposes'].count('COUNTER_OFFER') == 1


def test_pdf_attachments_flow_through_real_preprocessing_and_preserve_evidence():
    result = run_scenario(attachments_dir=Path(__file__).parent / 'fixtures/garlic_attachments')
    assert len(result['documents']) == 5
    assert all(d['status'] == 'READ' and d['page_count'] == 1 for d in result['documents'])
    assert all('.pdf p.1' in evidence['location'] for evidence in result['price_evidence'])
    assert result['card']['review_outcome'] == 'COMPLETED'
    assert min(int(p['total_amount']) for p in result['card']['plans']) == 9580000
    assert result['rounds'][-1]['draft_ids'] == result['card']['counter_offer']['draft_ids']


def test_missing_policy_still_builds_review_card_without_inventing_target():
    card = run_scenario(no_policy=True)['card']
    assert card['review_outcome'] == 'REVIEW_REQUIRED_CLOSED'
    assert card['counter_offer']['status'] == 'HOLD'
    assert card['counter_offer']['reference']['status'] == 'NO_POLICY'
    assert card['counter_offer']['target'] is None
    assert len(card['plans']) == 5


def test_missing_market_key_does_not_change_purchase_math_or_close_card():
    card = run_scenario(market=MarketReferenceService())['card']
    assert card['market_reference']['status'] == 'NOT_CONFIGURED'
    assert card['review_outcome'] == 'COMPLETED'
    assert min(int(p['total_amount']) for p in card['plans']) == 9580000
    assert card['counter_offer']['target']['target_unit_price'] == '9776'


def test_reference_gaps_require_same_product_and_keep_retail_label():
    today = datetime.now(KST)
    row = PUBLIC_ROW | {'exmn_ymd': today.strftime('%Y%m%d'), 'ww1_bfr_prc': '160000'}
    service = MarketReferenceService()
    service._load = lambda: ([row], today)
    quote = {'currency': 'KRW', 'unit': 'kg', 'unit_price': '10340',
             'evidence_jsonb': {'item_name': {'value': ITEM}},
             'checks_jsonb': {'specification': {'value': SPEC}}}
    result = service.get({'item_name': ITEM, 'specification': SPEC}, {
        'valid': quote, 'wrong-grade': quote | {'checks_jsonb': {'specification': {'value': '국산 대서 중품'}}},
        'wrong-unit': quote | {'unit': '박스'}, 'legacy': quote | {'checks_jsonb': []}})
    assert result['comparisons'] == [{'quote_id': 'valid', 'price_won_per_kg': '10340.00', 'difference_pct': '15.40'}]
    assert result['previous_prices']['week']['change_pct'] == '12.00'
    blueberry = PUBLIC_ROW | {'item_cd': '429', 'vrty_cd': '02', 'se_cd': '01', 'se_nm': '소매',
                              'exmn_dd_prc': '11567', 'unit_sz': '1'}
    result = reference_from_rows([blueberry], 'frozen_blueberry_imported', NOW)
    assert result['market_type'] == '소매'
    assert result['price_won_per_kg'] == '11567.00'
