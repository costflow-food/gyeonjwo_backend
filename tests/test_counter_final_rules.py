"""KAN-262: ⑤ PREVIOUS_UNKNOWN의 RECHECK_SOURCE는 인상 전 단가가 적힌 구절을 인용해야 한다."""
import json
import unittest

from langchain_core.messages import ToolMessage

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


if __name__ == '__main__':
    unittest.main()
