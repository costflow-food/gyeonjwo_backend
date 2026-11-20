"""비용 조건만 답한 회신의 추출 (KAN-337 ①, KAN-335 보류 원인).

운영 기록(LangSmith gyeonjwo-prod)에서 확인한 두 사례:
- 11-20 냉동 딸기(job ebaaca36): "배송비는 납품 1회당 520,000원 별도" 회신을 ①이 store_structured_mail을 한 번도 부르지
  않고 NO_QUOTE_DATA로 보류했다. 검증자가 두 번 "배송비를 저장하라"고 지적했지만 같은 답을 되풀이했다. 그 업체의 이전
  견적(v1)은 저장돼 있었다.
- 11-19 깐마늘(job 174c3ee6): 운송비가 세 줄(금액·산정 기준·부담 주체)에 나뉜 회신에서 ①이 금액 줄만 source_text로
  보냈고, 도구는 SOURCE_MISMATCH("원문 표기 그대로")로만 거절했다. ①은 무엇을 고칠지 몰라 다시 거절당하고 보류했다.

원인과 측정(2026-11-20, 운영 두 업무의 데이터를 메모리 저장소에 올려 ①을 운영 모델 gpt-6.1-terra로 다시 실행):
- ① 프롬프트의 "가장 먼저 지킬 규칙 1"이 "단가가 숫자로 적혀 있을 때만 저장한다. 단가가 없으면 store_structured_mail을
  부르지 않고 HOLD한다"여서, 뒤쪽 보류 표의 "단가 없이 비용 조건만 바꾼 회신도 견적이다"와 충돌했다.
- 수정 전: 딸기 8회 중 4회, 깐마늘 8회 중 5회 NO_QUOTE_DATA 보류.
- 코드 확인 재실행(_resent_reply_terms)과 비용 거절 사유만 넣었을 때: 딸기 8회 중 4회, 깐마늘 8회 중 2회 보류
  (재실행 안내를 받고도 9번 중 3번만 저장) → 이것만으로는 부족했다.
- 규칙 1을 고친 뒤: 딸기 10/10, 깐마늘 10/10 저장(깐마늘 3회는 COST_TEXT_OUTSIDE_SOURCE 거절 뒤 고쳐서 저장).
  견적이 정말 없는 회신("확인 후 회신드리겠습니다")은 3/3 보류, 인상 통보·견적 회신(PDF 포함)은 8/8 저장으로 그대로였다.
"""
import unittest
from unittest import mock
from uuid import uuid4

from langchain_core.messages import HumanMessage, ToolMessage

from app.agents import agents, tools
from app.agents.agents import AgentRunner, IdHandles, unsent_reply_mails
from app.agents.schemas import AgentNextAction as N, AgentRole, ExtractionIn, ExtractionOut, PriorityIn, PriorityOut
from app.agents.tools import CostTerm, EvidenceRef, _HINTS, _costs

REPLY, OTHER_REPLY, SUPPLIER, NEW_SUPPLIER = 'mail-reply', 'mail-new', 'sup-quoted', 'sup-new'
HOLD = ExtractionOut(next_action=N.HOLD, reason='이 메일에는 업무 품목의 견적 정보가 없어 저장하지 않았습니다.',
                     hold={'code': 'NO_QUOTE_DATA', 'field': None})
STORED = ExtractionOut(next_action=N.STORED, reason='운송비 조건을 저장했습니다.', quote_version_ids=['quote-2'])


class Store:
    """메일의 업체 연결과 업체별 이전 견적만 아는 저장소."""

    def __init__(self, quoted=(SUPPLIER,)):
        self.quoted, self.lookups = set(quoted), []

    def list_case_mails(self, case_id):
        return [{'mail_id': REPLY, 'supplier_id': SUPPLIER}, {'mail_id': OTHER_REPLY, 'supplier_id': NEW_SUPPLIER},
                {'mail_id': 'mail-unlinked', 'supplier_id': None}]

    def latest_quote_version(self, case_id, supplier_id):
        self.lookups.append(supplier_id)
        return ('quote-1', object()) if supplier_id in self.quoted else None


def extraction_input(mail_ids=(REPLY,), recheck_fields=()):
    return ExtractionIn(case_id=uuid4(), input_revision=5, mail_ids=list(mail_ids), recheck_fields=list(recheck_fields))


def tool_message(name, content='{}'):
    return ToolMessage(content=content, name=name, tool_call_id='call-1')


class UnsentReplyMailsTests(unittest.TestCase):
    def found(self, output=HOLD, messages=(), store=None, **kwargs):
        return unsent_reply_mails(AgentRole.INFO_EXTRACTOR, extraction_input(**kwargs), output, list(messages),
                                  store or Store())

    def test_direct_no_quote_hold_on_a_quoted_suppliers_reply(self):
        # 딸기 사례: 메일만 읽고 저장 도구 없이 보류했다
        messages = [tool_message('get_case_context'), tool_message('read_mail_evidence')]
        self.assertEqual(self.found(messages=messages), [REPLY])

    def test_supplier_without_an_earlier_quote_is_a_real_no_quote(self):
        # 이전 견적이 없는 업체의 단가 없는 메일은 도구도 NO_QUOTE_DATA로 거절한다. 다시 시키지 않는다
        self.assertEqual(self.found(mail_ids=[OTHER_REPLY]), [])
        self.assertEqual(self.found(mail_ids=['mail-unlinked']), [])
        self.assertEqual(self.found(store=Store(quoted=())), [])

    def test_only_the_quoted_suppliers_mails_are_named(self):
        self.assertEqual(self.found(mail_ids=[OTHER_REPLY, REPLY, 'mail-unlinked']), [REPLY])

    def test_any_store_attempt_settles_it(self):
        # 도구가 저장했거나 거절했으면 그 결과가 판단 근거다
        for content in ('{"status": "REJECTED", "rejected": [{"field": "unit_price", "code": "NO_QUOTE_DATA"}]}',
                        '{"status": "STORED", "quote_version_id": "quote-2"}'):
            with self.subTest(content):
                self.assertEqual(self.found(messages=[tool_message('store_structured_mail', content)]), [])

    def test_other_outcomes_are_left_alone(self):
        store = Store()
        self.assertEqual(self.found(output=STORED, store=store), [])
        other = ExtractionOut(next_action=N.HOLD, reason='x', hold={'code': 'SOURCE_READ_FAILED', 'field': None})
        self.assertEqual(self.found(output=other, store=store), [])
        self.assertEqual(self.found(output=ExtractionOut(next_action=N.HOLD, reason='x'), store=store), [])
        self.assertEqual(self.found(recheck_fields=['unit_price'], store=store), [])     # ③④⑤가 부른 재추출
        self.assertEqual(store.lookups, [])         # 해당하지 않으면 저장소를 읽지 않는다

    def test_other_roles_are_left_alone(self):
        data = PriorityIn(case_id=uuid4(), input_revision=1, quote_version_ids=['Q1'])
        output = PriorityOut(next_action=N.BUILD_PLANS, reason='검토 가능')
        self.assertEqual(unsent_reply_mails(AgentRole.INFO_PRIORITY, data, output, [], Store()), [])

    def test_supplier_is_looked_up_once(self):
        store = Store()

        class TwoMails(Store):
            def list_case_mails(self, case_id):
                return [{'mail_id': REPLY, 'supplier_id': SUPPLIER}, {'mail_id': 'mail-reply-2', 'supplier_id': SUPPLIER}]

        store = TwoMails()
        self.assertEqual(self.found(mail_ids=[REPLY, 'mail-reply-2'], store=store), [REPLY, 'mail-reply-2'])
        self.assertEqual(store.lookups, [SUPPLIER])


class ResendTests(unittest.TestCase):
    """AgentRunner: 해당하면 같은 대화에 코드 확인 안내를 붙여 한 번만 다시 실행한다."""

    def resend(self, first, second, store=None):
        calls = []

        def run(messages):
            calls.append(messages)
            if isinstance(second, Exception):
                raise second
            return second, [*messages, tool_message('store_structured_mail')]

        handles = IdHandles()
        handles.hide({'mail_ids': [REPLY]})          # 첫 입력에서 M1이 된 메일
        conversation = [HumanMessage('input'), tool_message('read_mail_evidence')]
        with mock.patch.object(tools, '_quote_store', store or Store()):
            result = AgentRunner._resent_reply_terms(AgentRole.INFO_EXTRACTOR, extraction_input(), handles, first,
                                                     conversation, run)
        return result, calls, conversation

    def test_resend_continues_the_same_conversation_with_the_fact(self):
        with self.assertLogs('gyeonjwo', level='INFO') as logs:
            (output, messages), calls, conversation = self.resend(HOLD, STORED)
        self.assertEqual(output, STORED)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][:-1], conversation)
        feedback = calls[0][-1]
        self.assertIsInstance(feedback, HumanMessage)
        # LLM에게는 메일 ID가 아니라 이름표(M1)로 알린다. 이전 견적이 있다는 사실과 할 일을 적는다
        self.assertIn('M1을(를) 보낸 업체는 이 업무에 이전 견적이 저장돼 있다', feedback.content)
        self.assertIn('store_structured_mail', feedback.content)
        self.assertNotIn(REPLY, feedback.content)
        self.assertIn('reply terms not sent to store', logs.output[0])
        self.assertEqual(messages[-1].name, 'store_structured_mail')      # 뒤따르는 검증은 다시 실행한 대화를 본다

    def test_second_hold_is_accepted(self):
        # 정말 저장할 값이 없는 회신("확인 후 회신드리겠습니다")이면 두 번째도 보류한다. 더 시키지 않는다
        with self.assertLogs('gyeonjwo', level='INFO'):
            (output, _), calls, _ = self.resend(HOLD, HOLD)
        self.assertEqual((output, len(calls)), (HOLD, 1))

    def test_no_resend_when_it_does_not_apply(self):
        (output, messages), calls, conversation = self.resend(STORED, HOLD)
        self.assertEqual((output, messages, calls), (STORED, conversation, []))
        (output, _), calls, _ = self.resend(HOLD, STORED, store=Store(quoted=()))
        self.assertEqual((output, calls), (HOLD, []))

    def test_rerun_error_is_raised(self):
        # 다시 실행하는 동안 견적이 저장됐을 수 있어 처음의 보류로 덮지 않는다
        with self.assertLogs('gyeonjwo', level='INFO'):
            with self.assertRaises(agents.UngroundedOutputError):
                self.resend(HOLD, agents.UngroundedOutputError('no final answer'))

    def test_runner_calls_it_before_the_verifier(self):
        order = []

        def fake_run_once(role, agent, data, context, messages, log):
            order.append('run')
            return HOLD, []

        def resent(role, data, handles, output, messages, run):
            order.append('resend')
            return STORED, ['rerun conversation']

        def verified(self, role, handles, output, messages, run):
            order.append(('verify', output, messages))
            return output

        with mock.patch.object(AgentRunner, 'agent_for', return_value=None), \
                mock.patch.object(AgentRunner, '_run_once', staticmethod(fake_run_once)), \
                mock.patch.object(AgentRunner, '_resent_reply_terms', staticmethod(resent)), \
                mock.patch.object(AgentRunner, '_verified', verified):
            output = AgentRunner(model=mock.Mock())(AgentRole.INFO_EXTRACTOR, extraction_input())
        self.assertEqual(output, STORED)
        self.assertEqual(order, ['run', 'resend', ('verify', STORED, ['rerun conversation'])])


class CostSourceTextTests(unittest.TestCase):
    """깐마늘 사례의 회신 본문. 운송비가 세 줄에 나뉘어 있다."""

    BODY = ('- 운송비 금액: 80,000원 (600kg 1회 납품 기준)\n- 산정 기준: 납품 1회당\n'
            '- 단가 포함 여부: 단가에 포함되지 않은 별도 청구 (구매자 부담)')

    def costs(self, source_text, **fields):
        def check(evidence, raw, numeric=False):
            """_SourceCheck처럼 공백을 빼고 본문에 있는지 본다."""
            text, value = tools._normalized(self.BODY, numeric), tools._normalized(raw, numeric)
            return (None, {'mail_id': REPLY, 'location': 'body'}) if tools._contains(text, value, numeric) \
                else ('SOURCE_MISMATCH', None)

        cost = CostTerm(cost_type='SHIPPING', source_text=source_text, included_in_price=False,
                        evidence=EvidenceRef(mail_id=REPLY), **fields)
        rejected = []
        return _costs(check, [cost], rejected, []), rejected

    def test_basis_outside_the_source_text_gets_its_own_code_and_hint(self):
        # ①이 실제로 보낸 인자: 금액 줄만 source_text로 보내고 basis·payer는 다음 줄에서 가져왔다
        stored, rejected = self.costs('운송비 금액: 80,000원 (600kg 1회 납품 기준)', amount='80,000원',
                                      basis='납품 1회당', basis_type='PER_ORDER', payer='구매자 부담')
        self.assertEqual((stored, rejected), ([], [{'field': 'cost_terms[0]', 'code': 'COST_TEXT_OUTSIDE_SOURCE'}]))
        hint = tools._with_hints({'status': 'REJECTED', 'rejected': rejected}, rejected)['hints']
        self.assertEqual(hint, {'COST_TEXT_OUTSIDE_SOURCE': _HINTS['COST_TEXT_OUTSIDE_SOURCE']})
        for word in ('source_text', '이어서', 'null'):
            self.assertIn(word, hint['COST_TEXT_OUTSIDE_SOURCE'])

    def test_joining_the_lines_as_the_hint_says_is_stored(self):
        stored, rejected = self.costs(self.BODY, amount='80,000원', basis='납품 1회당', basis_type='PER_ORDER',
                                      payer='구매자 부담')
        self.assertEqual(rejected, [])
        self.assertEqual((stored[0]['amount'], stored[0]['basis'], stored[0]['payer'], stored[0]['included_in_price']),
                         ('80000', '납품 1회당', '구매자 부담', False))

    def test_dropping_the_outside_values_as_the_hint_says_is_stored(self):
        stored, rejected = self.costs('운송비 금액: 80,000원 (600kg 1회 납품 기준)', amount='80,000원')
        self.assertEqual(rejected, [])
        self.assertEqual((stored[0]['amount'], stored[0]['basis'], stored[0]['payer']), ('80000', None, None))

    def test_source_text_not_in_the_mail_is_still_a_source_mismatch(self):
        _, rejected = self.costs('운송비는 90,000원입니다', amount='90,000원')
        self.assertEqual(rejected, [{'field': 'cost_terms[0]', 'code': 'SOURCE_MISMATCH'}])


class ExtractorPromptRuleTests(unittest.TestCase):
    """① 프롬프트의 첫 규칙이 비용 조건만 있는 회신의 저장을 막지 않는다(메일·통화 프롬프트 모두)."""

    def prompts(self):
        return [agents.system_prompt(AgentRole.INFO_EXTRACTOR, channels) for channels in
                (agents.MAIL_ONLY, frozenset({'CALL'}), frozenset({'MAIL', 'CALL'}))]

    def test_first_rule_no_longer_requires_a_unit_price(self):
        for prompt in self.prompts():
            self.assertNotIn('숫자로 적혀 있을 때만 저장한다', prompt)
            self.assertNotIn('   - 단가가 없다 (', prompt)

    def test_first_rule_sends_cost_only_replies_to_the_tool(self):
        for prompt in self.prompts():
            rule = prompt[prompt.index('1. 저장할지 먼저 정한다.'):prompt.index('   - 단가도 위의 견적 조건도 없다')]
            for phrase in ('단가가 없어도', '비용(운송비·배송비·하차비 등)·공급 가능량·납기·세금 조건이 하나라도',
                           'store_structured_mail', 'cost_terms', '이전 견적에서 이어받는다',
                           'NO_QUOTE_DATA로 거절하고, 그때 HOLD한다', '스스로 짐작해서 도구를 부르지 않고 HOLD하지 않는다'):
                self.assertIn(phrase, rule)


if __name__ == '__main__':
    unittest.main()
