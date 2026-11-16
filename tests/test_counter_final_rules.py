"""KAN-262: ⑤ PREVIOUS_UNKNOWN의 RECHECK_SOURCE는 인상 전 단가가 적힌 구절을 인용해야 한다."""
import json
import unittest

from langchain_core.messages import HumanMessage, ToolMessage

from app.agents.agents import _counter_final_rules
from app.agents.schemas import CounterOfferOut

CALC = ToolMessage(name='calculate_counter_offer', tool_call_id='c1', content=json.dumps(
    {'status': 'OK', 'counter_status': 'PREVIOUS_UNKNOWN', 'requested_unit_price': '5500'}))


def answer(action, source_text=None):
    recheck = [{'supplier_id': 'S1', 'field': 'previous_unit_price', 'mail_id': 'M1', 'source_text': source_text}] \
        if source_text else []
    return CounterOfferOut(next_action=action, reason='사유', recheck=recheck)


class CounterFinalRulesTests(unittest.TestCase):
    def test_requested_price_quote_is_sent_back(self):  # K07 r1·r8 실제 실패
        quote = '원가 상승으로 냉동 딸기 단가를 개당 5,500원(부가세 별도)으로 조정하게 되었습니다.'
        self.assertEqual(len(_counter_final_rules(answer('RECHECK_SOURCE', quote), [CALC])), 1)

    def test_previous_price_quote_passes(self):  # K06
        self.assertEqual(_counter_final_rules(answer('RECHECK_SOURCE', '기존 단가는 개당 5,200원이었습니다.'), [CALC]), [])

    def test_hold_passes(self):
        self.assertEqual(_counter_final_rules(answer('HOLD'), [CALC]), [])


# KAN-277: 입력·도구 결과 모두 이름표(S1·Q1) 기준. 비유지 안은 기존 업체가 포함된 SPLIT
SPLIT = [{'supplier_id': 'S1', 'quote_version_id': 'Q1', 'quantity': '400'},
         {'supplier_id': 'S2', 'quote_version_id': 'Q2', 'quantity': '600'}]


def case_input(plans):
    return HumanMessage(json.dumps({'validated_plans': plans}))


def calc(status, alternative):
    return ToolMessage(name='calculate_counter_offer', tool_call_id='c2', content=json.dumps(
        {'status': 'OK', 'counter_status': status, 'alternative': alternative}))


KEEP = {'plan_type': 'KEEP', 'validation_status': 'VALID', 'total_amount': '5400000',
        'allocations': [{'supplier_id': 'S1', 'quote_version_id': 'Q1', 'quantity': '1000'}]}
PLANS = [KEEP, {'plan_type': 'SPLIT', 'validation_status': 'VALID', 'total_amount': '5450000', 'allocations': SPLIT}]


class CounterAlternativeRuleTests(unittest.TestCase):
    def test_incumbent_share_dropped_is_sent_back(self):  # K08 r2·K07 r20 실제 실패
        messages = [case_input(PLANS), calc('NOT_COMPUTABLE', SPLIT[1:])]
        self.assertEqual(len(_counter_final_rules(answer('HOLD'), messages)), 1)

    def test_same_allocations_pass(self):  # K08 r1·r3
        messages = [case_input(PLANS), calc('NOT_NEEDED', SPLIT)]
        self.assertEqual(_counter_final_rules(answer('COUNTER_OFFER_SKIPPED'), messages), [])

    def test_no_alternative_plan_skips_check(self):  # K09: 유지안만 있으면 대안 검사 안 함
        messages = [case_input([KEEP]), calc('NOT_COMPUTABLE', SPLIT[1:])]
        self.assertEqual(_counter_final_rules(answer('HOLD'), messages), [])


if __name__ == '__main__':
    unittest.main()
