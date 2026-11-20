"""조회의 일시 오류 재시도 (KAN-339).

운영 사례(11-20 업무 c1eaf264): ④ 구매안 검증 도구의 조회 한 번이 실패해(PROCESSING_UNAVAILABLE) 업무 전체가
GRAPH_FAILED가 됐다. 같은 입력의 다른 14회는 정상이었고, 그 시각 Supabase 응답은 모두 200이었다.
"""
import unittest
from types import SimpleNamespace
from unittest import mock

import httpx

from app.core.errors import ServiceError
from app.features.work_cases.infrastructure.agent_store import AgentStore
from app.features.work_cases.infrastructure.graph_repository import GraphRuntimeRepository
from app.shared.infrastructure.db import read_retry
from app.shared.infrastructure.db.contracts import OwnershipLost
from app.shared.infrastructure.db.read_retry import READ_RETRY_PAUSES, read_with_retry, transient


class ApiError(Exception):
    """postgrest.APIError처럼 code를 가진 응답 오류."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


class Flaky:
    """앞의 errors를 차례로 올린 뒤 value를 돌려주는 조회."""

    def __init__(self, errors, value=None):
        self.errors, self.value, self.calls = list(errors), value, 0

    def __call__(self):
        self.calls += 1
        if self.errors:
            raise self.errors.pop(0)
        return self.value


class ReadWithRetryTests(unittest.TestCase):
    def run_read(self, read, **kwargs):
        pauses = []
        with self.assertLogs('gyeonjwo', level='WARNING') if read.errors else self.assertNoLogs('gyeonjwo', level='WARNING'):
            try:
                return read_with_retry(read, sleep=pauses.append, **kwargs), pauses
            except Exception as error:
                return error, pauses

    def test_success_reads_once(self):
        read = Flaky([], value=[1])
        self.assertEqual(self.run_read(read), ([1], []))
        self.assertEqual(read.calls, 1)

    def test_connection_error_is_retried(self):
        read = Flaky([httpx.RemoteProtocolError('Server disconnected')], value=[1])
        self.assertEqual(self.run_read(read), ([1], [READ_RETRY_PAUSES[0]]))
        self.assertEqual(read.calls, 2)

    def test_two_failures_then_success(self):
        read = Flaky([httpx.ReadTimeout('timeout'), ApiError('57014')], value=[1])
        self.assertEqual(self.run_read(read), ([1], list(READ_RETRY_PAUSES)))
        self.assertEqual(read.calls, 3)

    def test_gives_up_after_the_last_attempt(self):
        errors = [httpx.ConnectError('x'), httpx.ConnectError('y'), httpx.ConnectError('z')]
        read = Flaky(list(errors), value=[1])
        result, pauses = self.run_read(read)
        self.assertIs(result, errors[2])
        self.assertEqual((read.calls, pauses), (3, list(READ_RETRY_PAUSES)))

    def test_permanent_errors_are_not_retried(self):
        # 문법·권한·없는 열(42xxx·PGRST1xx·PGRST2xx)과 형식 오류는 다시 읽어도 같다
        for error in (ApiError('42703'), ApiError('PGRST204'), ValueError('Invalid rows')):
            with self.subTest(str(error)):
                read = Flaky([error], value=[1])
                with self.assertNoLogs('gyeonjwo', level='WARNING'):
                    with self.assertRaises(type(error)):
                        read_with_retry(read, sleep=lambda seconds: self.fail('slept'))
                self.assertEqual(read.calls, 1)

    def test_limit_check_runs_before_every_attempt_and_stops_the_retry(self):
        # 실행 시간 한도·임대 상실은 다시 시도하지 않고 그대로 올린다
        checks = []

        def before():
            checks.append(1)
            if len(checks) == 2:
                raise OwnershipLost('lost')

        read = Flaky([httpx.ReadTimeout('timeout')], value=[1])
        with self.assertLogs('gyeonjwo', level='WARNING'):
            with self.assertRaises(OwnershipLost):
                read_with_retry(read, before_attempt=before, sleep=lambda seconds: None)
        self.assertEqual((read.calls, len(checks)), (1, 2))

    def test_log_has_codes_only(self):
        read = Flaky([httpx.ReadTimeout('secret mail text')], value=[1])
        with self.assertLogs('gyeonjwo', level='WARNING') as logs:
            read_with_retry(read, label='agent_store', sleep=lambda seconds: None)
        self.assertEqual(logs.output, ['WARNING:gyeonjwo:db read retry label=agent_store attempt=1 '
                                       'error=ReadTimeout code=None'])

    def test_transient_classification(self):
        self.assertTrue(transient(httpx.RemoteProtocolError('x')))
        self.assertTrue(transient(httpx.PoolTimeout('x')))
        self.assertTrue(transient(ApiError('PGRST002')))
        self.assertFalse(transient(ApiError('23505')))
        self.assertFalse(transient(ServiceError('PROCESSING_UNAVAILABLE', 'x', 503)))


class Query:
    def __init__(self, read):
        self.read = read

    def __getattr__(self, name):
        return lambda *args, **kwargs: self

    def execute(self):
        return SimpleNamespace(data=self.read())


class StoreReadTests(unittest.TestCase):
    """도구가 쓰는 조회 두 곳(AgentStore.rows, GraphRuntimeRepository.context)에 재시도가 걸려 있다."""

    def setUp(self):
        patcher = mock.patch.object(read_retry.time, 'sleep')
        self.slept = patcher.start()
        self.addCleanup(patcher.stop)

    def store(self, checks=None):
        checks = [] if checks is None else checks
        store = AgentStore.__new__(AgentStore)
        store.scope = SimpleNamespace(remaining_seconds=lambda: checks.append(1))
        return store

    def test_rows_survives_one_dropped_connection(self):
        read = Flaky([httpx.RemoteProtocolError('Server disconnected')], value=[{'id': 1}])
        checks = []
        with self.assertLogs('gyeonjwo', level='WARNING'):
            self.assertEqual(self.store(checks).rows(Query(read)), [{'id': 1}])
        self.assertEqual((read.calls, len(checks)), (2, 2))
        self.slept.assert_called_once_with(READ_RETRY_PAUSES[0])

    def test_rows_reports_processing_unavailable_after_all_attempts(self):
        read = Flaky([httpx.ConnectError('x')] * 3)
        with self.assertLogs('gyeonjwo', level='WARNING'):
            with self.assertRaises(ServiceError) as raised:
                self.store().rows(Query(read))
        self.assertEqual((raised.exception.code, read.calls), ('PROCESSING_UNAVAILABLE', 3))

    def test_rows_keeps_rejecting_a_non_list_result(self):
        read = Flaky([], value={'kind': 'ERROR'})
        with self.assertRaises(ServiceError) as raised:
            self.store().rows(Query(read))
        self.assertEqual((raised.exception.code, read.calls), ('PROCESSING_UNAVAILABLE', 1))

    def test_rows_passes_lease_loss_through(self):
        store = AgentStore.__new__(AgentStore)

        def lost():
            raise OwnershipLost('lost')

        store.scope = SimpleNamespace(remaining_seconds=lost)
        with self.assertRaises(OwnershipLost):
            store.rows(Query(Flaky([], value=[])))

    def context(self, read):
        repo = GraphRuntimeRepository.__new__(GraphRuntimeRepository)
        repo.client = SimpleNamespace(table=lambda name: Query(read))
        return repo.context('owner-1', 'case-1', 2, 'batch-1')

    ROW = {'id': 'case-1', 'input_revision': 2, 'current_batch_id': 'batch-1', 'status': 'Running',
           'processing_step': 'GENERATING_PLANS', 'archived_at': None, 'decision': None}

    def test_context_survives_one_timeout(self):
        read = Flaky([httpx.ReadTimeout('timeout')], value=[self.ROW])
        with self.assertLogs('gyeonjwo', level='WARNING'):
            self.assertEqual(self.context(read), self.ROW)
        self.assertEqual(read.calls, 2)

    def test_context_still_reports_a_changed_revision_without_retry(self):
        read = Flaky([], value=[self.ROW | {'input_revision': 3}])
        with self.assertRaises(ServiceError) as raised:
            self.context(read)
        self.assertEqual((raised.exception.code, read.calls), ('REVISION_CONFLICT', 1))


class RealClientTests(unittest.TestCase):
    """실제 DB 클라이언트(postgrest)에서도 성립하는지: 연결 오류가 httpx 예외 그대로 올라오고(재시도 대상으로 분류),
    같은 질의 객체를 다시 실행하면 같은 요청이 나간다. 서버가 쓰는 요청 관찰(observe_data_api)도 그대로 붙인다."""

    def client(self, handler):
        from postgrest import SyncPostgrestClient
        from app.core.upstream_logs import observe_data_api
        client = SyncPostgrestClient('http://db.test/rest/v1',
                                     http_client=httpx.Client(transport=httpx.MockTransport(handler)))
        observe_data_api(client.session)
        return client

    def test_dropped_connection_then_success_through_agent_store(self):
        seen = []

        def handler(request):
            seen.append((request.method, str(request.url)))
            if len(seen) == 1:
                raise httpx.RemoteProtocolError('Server disconnected without sending a response.', request=request)
            return httpx.Response(200, json=[{'id': 'q-1'}])

        query = (self.client(handler).table('quote_versions').select('id').eq('work_case_id', 'case-1')
                 .order('version_no', desc=True).limit(1))
        store = AgentStore.__new__(AgentStore)
        store.scope = SimpleNamespace(remaining_seconds=lambda: None)
        with mock.patch.object(read_retry.time, 'sleep') as slept, self.assertLogs('gyeonjwo', level='WARNING') as logs:
            self.assertEqual(store.rows(query), [{'id': 'q-1'}])
        self.assertEqual(len(seen), 2)
        self.assertEqual(seen[0], seen[1])                       # 다시 실행해도 같은 요청이다
        self.assertEqual(seen[0][0], 'GET')
        slept.assert_called_once_with(READ_RETRY_PAUSES[0])
        self.assertTrue(any('db read retry label=agent_store attempt=1 error=RemoteProtocolError' in line
                            for line in logs.output))

    def test_read_timeout_is_transient_and_api_errors_are_not_retried(self):
        def timeout(request):
            raise httpx.ReadTimeout('timed out', request=request)

        with self.assertRaises(httpx.ReadTimeout) as raised, self.assertLogs('gyeonjwo', level='WARNING'):
            self.client(timeout).table('work_cases').select('id').limit(1).execute()
        self.assertTrue(transient(raised.exception))

        seen = []

        def missing_column(request):
            seen.append(1)
            return httpx.Response(400, json={'code': '42703', 'message': 'column does not exist', 'details': None,
                                             'hint': None})

        store = AgentStore.__new__(AgentStore)
        store.scope = SimpleNamespace(remaining_seconds=lambda: None)
        with self.assertRaises(ServiceError) as raised:
            store.rows(self.client(missing_column).table('work_cases').select('nope').limit(1))
        self.assertEqual((raised.exception.code, len(seen)), ('PROCESSING_UNAVAILABLE', 1))

    def test_statement_timeout_response_is_retried(self):
        seen = []

        def handler(request):
            seen.append(1)
            if len(seen) == 1:
                return httpx.Response(500, json={'code': '57014', 'message': 'canceling statement due to statement timeout',
                                                 'details': None, 'hint': None})
            return httpx.Response(200, json=[])

        store = AgentStore.__new__(AgentStore)
        store.scope = SimpleNamespace(remaining_seconds=lambda: None)
        with mock.patch.object(read_retry.time, 'sleep'), self.assertLogs('gyeonjwo', level='WARNING'):
            self.assertEqual(store.rows(self.client(handler).table('work_cases').select('id').limit(1)), [])
        self.assertEqual(len(seen), 2)


if __name__ == '__main__':
    unittest.main()
