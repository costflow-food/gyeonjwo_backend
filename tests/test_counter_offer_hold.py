"""⑤ COUNTER_OFFER HOLD must keep its own reason, not a guessed MISSING_FIELD.

Since 2026-11-16 ⑤ no longer emits NO_POLICY (company policy dropped). The NO_POLICY
test below covers runs saved before that, which held cases still read their reason from.

E2E 2026-11-16 (case 38aeba1b): ⑤ held with reference.status NO_POLICY and the
reason "…역제안 단가 범위를 정할 근거가 없습니다…"; the free-text fallback read
"단가 … 없" and stored MISSING_FIELD/unit_price ("견적 단가가 확인되지 않아…").
"""
from unittest import TestCase

from app.features.work_cases.domain.hold import MESSAGES, from_agent_output

NO_POLICY_REASON = ('회사 정책이 확인되지 않아 허용 가능한 역제안 단가 범위를 정할 근거가 없습니다. '
                    '검증된 대안은 있으나 정책 확인 전에는 목표 단가를 제시하지 않습니다.')


def counter_offer(status, reason=NO_POLICY_REASON):
    return {'next_action': 'HOLD', 'reason': reason, 'target': None, 'draft_ids': [], 'recheck': [],
            'reference': {'status': status, 'unit': 'kg', 'requested_unit_price': '4950',
                          'previous_unit_price': '4500', 'alternative_total': '3560000'}}


class CounterOfferHoldTests(TestCase):
    def test_saved_no_policy_run_is_not_a_missing_unit_price(self):
        hold = from_agent_output(counter_offer('NO_POLICY')).for_storage()
        self.assertEqual(hold, {'code': 'COUNTER_OFFER_POLICY_MISSING',
                                'message': MESSAGES['COUNTER_OFFER_POLICY_MISSING']})

    def test_not_computable(self):
        hold = from_agent_output(counter_offer('NOT_COMPUTABLE', '비교할 대안 총액을 계산할 수 없습니다.'))
        self.assertEqual(hold.code, 'COUNTER_OFFER_NOT_COMPUTABLE')

    def test_other_reference_status_keeps_existing_rules(self):
        # Statuses that are not ⑤ holds fall through to the old rules unchanged.
        hold = from_agent_output(counter_offer('PROPOSABLE', '견적 단가가 확인되지 않아 보류합니다.'))
        self.assertEqual((hold.code, hold.for_storage().get('field')), ('MISSING_FIELD', 'unit_price'))

    def test_structured_hold_still_wins(self):
        output = counter_offer('NO_POLICY') | {'hold': {'code': 'MISSING_FIELD', 'field': 'quantity'}}
        self.assertEqual(from_agent_output(output).for_storage()['field'], 'quantity')

    def test_extraction_hold_without_reference_unchanged(self):
        output = {'next_action': 'HOLD', 'reason': '원문에 단가가 없어 저장하지 못했습니다.'}
        self.assertEqual(from_agent_output(output).for_storage(),
                         {'code': 'MISSING_FIELD', 'message': '견적 단가가 확인되지 않아 검토를 보류했어요.',
                          'field': 'unit_price'})
