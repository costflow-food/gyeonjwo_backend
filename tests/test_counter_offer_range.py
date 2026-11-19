"""⑤ 역제안 범위: 회사 정책 없이 [max(유지 상한, 인상 전 단가), 요청 단가], 목표 단가는 그 하한 (2026-11-16 팀 결정).

정책값이 없는 업무(policy_jsonb {"source": "DEFAULT_UNCONFIRMED"})에서 ⑤가 유지 상한까지 계산하고도
NO_POLICY로 보류하던 문제. 수치는 dev 업무의 최신 견적 그대로다.
- 브로콜리 1d3970a4: 600kg, 초록들식품 4,400원 + 운송비 25,000원/회(인상 전 4,000원),
  대안 늘푸른팜 4,180원(운송비 포함) → 대안 총액 2,508,000원, 유지 상한 4,138.33원
- 딸기 38aeba1b: 800kg, 달콤농원 4,950원 + 운송비 20,000원/회(인상 전 4,500원),
  대안 프레시팜 4,400원 + 운송비 40,000원/회 → 대안 총액 3,560,000원, 유지 상한 4,425원(인상 전보다 낮음)
"""
import unittest
from decimal import Decimal

from app.agents.tools import (AlternativeAllocation, QuoteVersionRecord, RunContext, check_counter_target,
                              counter_offer_numbers, counter_range, revalidate_counter_offer, save_counter_offer_draft)

# DB internal.graph_check_refs가 초안 근거로 받는 종류. 이 밖의 종류는 FOREIGN_GRAPH_EVIDENCE로 초안 저장이 거절된다
GRAPH_EVIDENCE_KINDS = {'supplier_items', 'suppliers', 'purchase_history', 'agent_runs', 'quote_versions', 'work_cases'}

CASE_ID = 'case-counter'


def quote(supplier_id, unit_price, available, shipping):
    """최신 견적 한 행. shipping이 None이면 운송비 단가 포함, 숫자면 주문 한 번마다 별도."""
    cost = ({'cost_type': 'SHIPPING', 'amount': None, 'included_in_price': True, 'basis_type': None}
            if shipping is None else
            {'cost_type': 'SHIPPING', 'amount': str(shipping), 'included_in_price': False, 'basis_type': 'PER_ORDER'})
    return QuoteVersionRecord(
        work_case_id=CASE_ID, supplier_id=supplier_id, source_mail_id=f'mail-{supplier_id}', version_no=1,
        supersedes_id=None, unit_price=str(unit_price), unit='kg', shipping_cost=None if shipping is None else str(shipping),
        available_quantity=str(available), effective_from=None, valid_until=None,
        checks_jsonb={'tax_basis': {'value': 'EXCLUDED'}}, pricing_terms_jsonb={}, cost_terms_jsonb=[cost],
        item_notes_jsonb=[], evidence_jsonb={}, extraction_status='CONFIRMED', mail_normalized={}, case_conditions=None)


class Store:
    """⑤ 계산이 읽는 것만 가진 가짜 저장소. 업무 정책은 실제 dev 업무처럼 정해지지 않은 값이다."""

    def __init__(self, quantity, incumbent, alternative, previous):
        self.quotes = {'q-in': incumbent, 'q-alt': alternative}
        self.case = {'id': CASE_ID, 'input_revision': 1, 'quantity': str(quantity), 'unit': 'kg',
                     'policy': {'source': 'DEFAULT_UNCONFIRMED'}, 'latest_quote_version_ids': list(self.quotes)}
        self.previous = previous

    def get_work_case(self, case_id):
        return dict(self.case)

    def list_case_mails(self, case_id):
        return [{'mail_id': 'mail-in', 'message_role': 'INITIAL', 'supplier_id': 'incumbent'}]

    def get_quote_versions(self, case_id, ids):
        return [(qid, self.quotes[qid]) for qid in ids if qid in self.quotes]

    def get_increase_terms(self, case_id, supplier_id):
        if self.previous is None:
            return None
        return {'previous_unit_price': str(self.previous), 'increase_reason': '원물 수급 악화', 'mail_id': 'mail-in'}

    def get_supplier(self, case_id, supplier_id):
        return {'id': supplier_id, 'name': '초록들식품', 'contact_email': 'incumbent@example.com'}

    def current_send_settings(self, case_id):
        return {}

    def insert_mail_draft(self, record, expected_input_revision):
        self.draft = record
        return ('draft-1',)


def broccoli(previous=4000):
    return Store(600, quote('incumbent', 4400, 600, 25000), quote('alt', 4180, 700, None), previous)


def strawberry(previous=4500):
    return Store(800, quote('incumbent', 4950, 800, 20000), quote('alt', 4400, 800, 40000), previous)


def alternative(quantity):
    return [AlternativeAllocation(supplier_id='alt', quote_version_id='q-alt', quantity=str(quantity))]


CONTEXT = RunContext(CASE_ID, 1)


class CounterRangeTests(unittest.TestCase):
    def test_broccoli_lower_bound_is_alternative_ceiling(self):
        numbers = counter_offer_numbers(broccoli(), CONTEXT, alternative(600))
        self.assertEqual(numbers['counter_status'], 'PROPOSABLE')
        self.assertEqual(numbers['alternative_total'], '2508000')
        self.assertEqual(Decimal(numbers['ceiling_unit_price']).quantize(Decimal('0.01')), Decimal('4138.33'))
        self.assertEqual(numbers['allowed_range'], ['4139', '4400'])
        self.assertEqual(numbers['target_unit_price'], '4139')
        self.assertFalse(numbers['above_alternative'])
        self.assertFalse(numbers['skip_allowed'])

    def test_strawberry_lower_bound_is_previous_price(self):
        numbers = counter_offer_numbers(strawberry(), CONTEXT, alternative(800))
        self.assertEqual(numbers['counter_status'], 'PROPOSABLE')
        self.assertEqual(numbers['alternative_total'], '3560000')
        self.assertEqual(numbers['ceiling_unit_price'], '4425')
        self.assertEqual(numbers['allowed_range'], ['4500', '4950'])
        self.assertEqual(numbers['target_unit_price'], '4500')
        self.assertTrue(numbers['above_alternative'])

    def test_no_policy_status_is_gone(self):
        for store, quantity in ((broccoli(), 600), (strawberry(), 800)):
            numbers = counter_offer_numbers(store, CONTEXT, alternative(quantity))
            self.assertNotIn(numbers['counter_status'], ('NO_POLICY', 'WITHIN_POLICY'))

    def test_lower_bound_target_is_valid(self):
        self.assertEqual(check_counter_target(broccoli(), CONTEXT, '4139', alternative(600))['status'], 'VALID')
        self.assertEqual(check_counter_target(strawberry(), CONTEXT, '4500', alternative(800))['status'], 'VALID')

    def test_any_other_target_is_rejected(self):
        for price in ('4138', '4200', '4400'):
            checked = check_counter_target(broccoli(), CONTEXT, price, alternative(600))
            self.assertEqual(checked['violations'], [{'code': 'NOT_LOWER_BOUND', 'target_unit_price': '4139',
                                                      'allowed_range': ['4139', '4400']}])
        self.assertEqual(check_counter_target(strawberry(), CONTEXT, '4700', alternative(800))['status'], 'INVALID')

    def test_request_cheaper_than_alternative_is_accepted(self):
        store = broccoli()
        store.quotes['q-in'] = quote('incumbent', 4100, 600, 25000)
        numbers = counter_offer_numbers(store, CONTEXT, alternative(600))
        self.assertEqual((numbers['counter_status'], numbers['skip_allowed']), ('NOT_NEEDED', True))

    def test_previous_unknown_is_unchanged(self):
        numbers = counter_offer_numbers(broccoli(previous=None), CONTEXT, alternative(600))
        self.assertEqual(numbers['counter_status'], 'PREVIOUS_UNKNOWN')

    def test_price_check_node_passes_target_without_policy(self):
        plans = [{'plan_type': 'SWITCH', 'validation_status': 'VALID', 'total_amount': '2508000',
                  'allocations': [{'supplier_id': 'alt', 'quote_version_id': 'q-alt', 'quantity': '600'}]}]
        target = {'target_unit_price': '4139', 'alternative': plans[0]['allocations']}
        self.assertEqual(revalidate_counter_offer(broccoli(), CASE_ID, 1, target, False, plans)['check'], 'PASS')
        self.assertEqual(revalidate_counter_offer(broccoli(), CASE_ID, 1, None, True, plans)['check'], 'FIX')


class CounterDraftEvidenceTests(unittest.TestCase):
    """2026-11-17 운영(업무 08ed48ca): 초안 근거에 mail_messages 참조가 들어가 DB가 FOREIGN_GRAPH_EVIDENCE로 거절했다."""

    def test_draft_evidence_kinds_are_accepted_by_the_db(self):
        store = broccoli()
        context = RunContext(CASE_ID, 1, '00000000-0000-4000-8000-000000000001')
        result = save_counter_offer_draft(store, context, '4139', [], alternative(600))
        self.assertEqual(result['status'], 'DRAFTED')
        kinds = {ref.split(':', 1)[0] for ref in store.draft.evidence_refs_jsonb}
        self.assertLessEqual(kinds, GRAPH_EVIDENCE_KINDS)
        self.assertIn('quote_versions', kinds)


class CounterDraftQuantityTests(unittest.TestCase):
    """2026-11-20 E2E(냉동 망고 15회): ⑤가 conditions를 비우면 역제안 초안에 업무 수량이 빠졌다(15/15).
    수량은 LLM 조건이 아니라 업무 값으로 항상 적는다."""

    def test_draft_states_case_quantity_without_conditions(self):
        store = broccoli()
        result = save_counter_offer_draft(store, CONTEXT, '4139', [], alternative(600))
        self.assertEqual(result['status'], 'DRAFTED')
        self.assertIn('- 수량: 600kg', store.draft.body_text)
        lines = store.draft.body_text.splitlines()
        self.assertEqual(lines.index('- 수량: 600kg') + 1, lines.index('- 단가: 4,139원/kg'))

    def test_thousands_separator_matches_rfq(self):
        store = Store(2000, quote('incumbent', 4900, 2000, None), quote('alt', 4600, 2000, None), 4500)
        result = save_counter_offer_draft(store, CONTEXT, '4600', [], alternative(2000))
        self.assertEqual(result['status'], 'DRAFTED')
        self.assertIn('- 수량: 2,000kg', store.draft.body_text)


class CounterRangeFunctionTests(unittest.TestCase):
    def test_lower_bound_rounds_up_to_whole_won(self):
        self.assertEqual(counter_range(Decimal('4400'), Decimal('4000'), Decimal('4138.3333')),
                         (Decimal('4139'), Decimal('4400'), False))

    def test_previous_above_ceiling_marks_above_alternative(self):
        self.assertEqual(counter_range(Decimal('4950'), Decimal('4500'), Decimal('4425')),
                         (Decimal('4500'), Decimal('4950'), True))

    def test_lower_bound_never_exceeds_request(self):
        low, high, _ = counter_range(Decimal('4400'), Decimal('4500'), Decimal('4300'))
        self.assertEqual((low, high), (Decimal('4400'), Decimal('4400')))


if __name__ == '__main__':
    unittest.main()
