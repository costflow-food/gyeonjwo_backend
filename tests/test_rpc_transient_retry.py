"""KAN-329: DB가 취소한 문장(57014 등)은 한 번만 다시 부른다."""
import unittest
from unittest.mock import patch

from postgrest.exceptions import APIError

from app.core.errors import ServiceError
from app.features.work_cases.infrastructure import runtime_repository
from app.features.work_cases.infrastructure.runtime_repository import CaseRuntimeRepository


def db_error(code, message='canceling statement due to statement timeout'):
    return APIError({'code': code, 'message': message, 'details': None, 'hint': None})


class Client:
    """rpc(...).execute() 를 부를 때마다 outcomes 를 차례로 돌려준다(예외면 던진다)."""

    def __init__(self, *outcomes):
        self.outcomes, self.calls = list(outcomes), []

    def rpc(self, name, params):
        self.calls.append((name, params))
        return self

    def execute(self):
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return type('Response', (), {'data': outcome})()


class TransientRetryTests(unittest.TestCase):
    def setUp(self):
        sleeper = patch.object(runtime_repository, 'sleep')
        self.sleep = sleeper.start()
        self.addCleanup(sleeper.stop)

    def test_statement_timeout_is_retried_once_and_succeeds(self):
        for state in ('57014', '55P03', '40P01'):
            with self.subTest(state=state):
                client = Client(db_error(state), {'kind': 'STORED'})
                result = CaseRuntimeRepository(client).call('store_case_graph_quote', p_record={'a': 1})
                self.assertEqual(result, {'kind': 'STORED'})
                self.assertEqual(client.calls, [('store_case_graph_quote', {'p_record': {'a': 1}})] * 2)
        self.assertEqual(self.sleep.call_count, 3)

    def test_second_timeout_fails_with_the_sqlstate_recorded(self):
        client = Client(db_error('57014'), db_error('57014'))
        with self.assertRaises(ServiceError) as raised:
            CaseRuntimeRepository(client).call('store_case_graph_quote')
        self.assertEqual(raised.exception.code, 'PROCESSING_UNAVAILABLE')
        self.assertEqual(raised.exception.cause, {'db_sqlstate': '57014'})
        self.assertEqual(len(client.calls), 2)

    def test_other_errors_are_not_retried(self):
        cases = [db_error('23514', 'REVIEW_SUPPLIER_NOT_AUTHORIZED'), db_error('40001', 'CANDIDATES_CHANGED'),
                 db_error('PGRST202', 'missing'), RuntimeError('connection reset'), TimeoutError('no response')]
        for error in cases:
            with self.subTest(error=repr(error)):
                client = Client(error, {'kind': 'STORED'})
                with self.assertRaises(ServiceError):
                    CaseRuntimeRepository(client).call('store_case_graph_quote')
                self.assertEqual(len(client.calls), 1)
        self.sleep.assert_not_called()

    def test_success_is_not_repeated(self):
        client = Client({'kind': 'STORED'})
        self.assertEqual(CaseRuntimeRepository(client).call('x'), {'kind': 'STORED'})
        self.assertEqual(len(client.calls), 1)
        self.sleep.assert_not_called()


if __name__ == '__main__':
    unittest.main()
