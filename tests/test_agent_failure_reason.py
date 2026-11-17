"""에이전트 실패 원인 기록: agent_runs.error_jsonb에 AGENT_FAILED만 남아 원인을 추적할 수 없던 문제.

11-16 블루베리 재시도에서 ③이 106초 만에 실패했지만 DB에는 {"code": "AGENT_FAILED"}만 남았다(BUG-010).
실행기가 코드형 원인(단계·예외 종류·오류 코드·DB 코드·마지막 도구)만 만들어 넘기고, 메일 원문·예외 메시지·도구 인자는 넘기지 않는다.
"""
import unittest

from app.agents.agents import UngroundedOutputError, failure_summary
from app.core.errors import ServiceError, code_token, db_error_cause
from app.features.work_cases.infrastructure.graph_store import failure_error
from app.features.work_cases.infrastructure.runtime_repository import CaseRuntimeRepository

RAW_MAIL = '안녕하세요 들녘농산입니다. 단가 8,800원'


class DbError(Exception):
    """PostgREST APIError처럼 code(SQLSTATE)·message를 가진 예외."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code, self.message = code, message


class Client:
    """rpc(...).execute()가 정해진 예외를 올리는 가짜 Supabase 클라이언트."""

    def __init__(self, error):
        self.error = error

    def rpc(self, name, params):
        return self

    def execute(self):
        raise self.error


class FailureSummaryTests(unittest.TestCase):
    def test_db_rejection_in_a_tool_keeps_codes_only(self):
        error = ServiceError('PROCESSING_UNAVAILABLE', RAW_MAIL, 503,
                             cause={'db_sqlstate': '23514', 'db_code': 'REVIEW_SUPPLIER_NOT_AUTHORIZED'})
        calls = [{'tool': 'get_case_context', 'args': {}, 'result': {'status': 'OK'}},
                 {'tool': 'create_clarification_draft', 'args': {'body': RAW_MAIL}, 'error': 'ServiceError'}]
        self.assertEqual(failure_summary(error, calls), {
            'code': 'AGENT_FAILED', 'stage': 'TOOL', 'error_type': 'ServiceError', 'error_code': 'PROCESSING_UNAVAILABLE',
            'db_sqlstate': '23514', 'db_code': 'REVIEW_SUPPLIER_NOT_AUTHORIZED',
            'last_tool': 'create_clarification_draft', 'tool_calls': 2})

    def test_final_check_failure(self):
        summary = failure_summary(UngroundedOutputError(f'quote {RAW_MAIL} was not stored'),
                                  [{'tool': 'store_structured_mail', 'result': {'status': 'STORED'}}])
        self.assertEqual((summary['stage'], summary['error_type'], summary['last_tool']),
                         ('FINAL_CHECK', 'UngroundedOutputError', 'store_structured_mail'))
        self.assertNotIn(RAW_MAIL, str(summary))

    def test_tool_call_limit(self):
        ToolCallLimitExceededError = type('ToolCallLimitExceededError', (Exception,), {})
        self.assertEqual(failure_summary(ToolCallLimitExceededError('limit'), [])['stage'], 'TOOL_LIMIT')

    def test_model_error_without_tools(self):
        self.assertEqual(failure_summary(RuntimeError(RAW_MAIL), []), {
            'code': 'AGENT_FAILED', 'stage': 'OTHER', 'error_type': 'RuntimeError', 'tool_calls': 0})


class FailureErrorTests(unittest.TestCase):
    def test_only_known_keys_with_code_values_are_sent(self):
        sent = failure_error({'stage': 'TOOL', 'error_type': RAW_MAIL, 'last_tool': 'read_mail_evidence',
                              'message': RAW_MAIL, 'tool_calls': 3, 'partial_tool_calls': [{'args': RAW_MAIL}]})
        self.assertEqual(sent, {'code': 'AGENT_FAILED', 'stage': 'TOOL', 'last_tool': 'read_mail_evidence',
                                'tool_calls': 3})

    def test_missing_or_bad_error_still_has_the_code(self):
        for error in (None, 'text', {'tool_calls': True}, {'tool_calls': -1}):
            self.assertEqual(failure_error(error), {'code': 'AGENT_FAILED'})


class DbErrorCauseTests(unittest.TestCase):
    def test_code_like_message_is_kept(self):
        self.assertEqual(db_error_cause(DbError('23514', 'HOLD_AFTER_QUOTE_STORED')),
                         {'db_sqlstate': '23514', 'db_code': 'HOLD_AFTER_QUOTE_STORED'})

    def test_sentence_message_is_dropped(self):
        self.assertEqual(db_error_cause(DbError('40001', 'terminal role immutable')), {'db_sqlstate': '40001'})
        self.assertEqual(db_error_cause(ValueError(RAW_MAIL)), {})

    def test_repository_attaches_the_cause_but_not_the_message(self):
        repo = CaseRuntimeRepository(Client(DbError('23514', 'SUPPLIER_MATCH_AVAILABLE')))
        with self.assertRaises(ServiceError) as raised:
            repo.call('finish_case_graph_agent_run')
        self.assertEqual((raised.exception.code, raised.exception.cause),
                         ('PROCESSING_UNAVAILABLE', {'db_sqlstate': '23514', 'db_code': 'SUPPLIER_MATCH_AVAILABLE'}))

    def test_code_token(self):
        self.assertEqual([code_token(v) for v in ('ServiceError', '23514', 'a b', '', None, 'x' * 65)],
                         ['ServiceError', '23514', None, None, None, None])


if __name__ == '__main__':
    unittest.main()
