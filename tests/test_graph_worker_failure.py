"""그래프 job 실패 기록 (#187 후속).

#187은 임대를 잃은 실행을 WORKER_INTERRUPTED로 기록한다. 그런데 운영 DB fail_case_graph_run은 임대가 아직 남아 있으면
WORKER_INTERRUPTED를 받지 않고 false를 돌려준다. 상실을 판단한 시점(갱신 실패 60초·DB가 소유권 변경을 알림)에는 임대가
대개 남아 있어, job이 recover_case_graph_runs가 정리할 때까지 최대 약 2분 PROCESSING으로 남았다.
"""
import logging
import unittest
from types import SimpleNamespace

from app.core.errors import ServiceError
from app.features.work_cases.application.graph_worker import GraphWorker
from app.shared.infrastructure.db.contracts import OwnershipLost

JOB = {'job_id': 'job-1', 'claim_token': 'token-1'}


class Repo:
    """fail_case_graph_run 가짜. 코드별 반환값을 정해 두고 호출을 기록한다."""

    def __init__(self, accepts, changed=False):
        self.accepts, self.calls, self.changed = accepts, [], changed

    def fail(self, job, code):
        self.calls.append(code)
        return self.accepts.get(code, True)

    def input_changed(self, job):
        if isinstance(self.changed, Exception):
            raise self.changed
        return self.changed


def record(error, accepts, changed=False):
    repo = Repo(accepts, changed)
    code = GraphWorker.record_failure(SimpleNamespace(repo=repo), JOB, error)
    return code, repo.calls


class RecordFailureTests(unittest.TestCase):
    def test_lease_loss_is_recorded_as_worker_interrupted_when_accepted(self):
        self.assertEqual(record(OwnershipLost('lost'), {}), ('WORKER_INTERRUPTED', ['WORKER_INTERRUPTED']))

    def test_rejected_worker_interrupted_is_closed_as_graph_failed(self):
        with self.assertLogs('uvicorn.error.case_graph', level='WARNING') as logs:
            code, calls = record(OwnershipLost('lost'), {'WORKER_INTERRUPTED': False})
        self.assertEqual((code, calls), ('GRAPH_FAILED', ['WORKER_INTERRUPTED', 'GRAPH_FAILED']))
        self.assertIn('WORKER_INTERRUPTED not accepted, retried as GRAPH_FAILED accepted=True job_id=job-1',
                      logs.output[0])

    def test_retry_result_is_logged_when_the_token_moved(self):
        # 다른 워커가 이미 가져갔으면 두 코드 모두 거절된다. 로그에 accepted=False로 남는다
        with self.assertLogs('uvicorn.error.case_graph', level='WARNING') as logs:
            code, calls = record(OwnershipLost('lost'), {'WORKER_INTERRUPTED': False, 'GRAPH_FAILED': False})
        self.assertEqual(calls, ['WORKER_INTERRUPTED', 'GRAPH_FAILED'])
        self.assertIn('accepted=False', logs.output[0])

    def test_worker_shutdown_keeps_the_existing_single_call(self):
        # 워커 종료(ServiceError WORKER_INTERRUPTED)는 이 PR 범위 밖이라 거절돼도 다시 기록하지 않는다(만료 뒤 회수)
        error = ServiceError('WORKER_INTERRUPTED', '검토 워커가 중단됐습니다.', 503)
        self.assertEqual(record(error, {'WORKER_INTERRUPTED': False}), ('WORKER_INTERRUPTED', ['WORKER_INTERRUPTED']))

    def test_service_error_codes_map_as_before(self):
        cases = {'REVISION_CONFLICT': 'INPUT_REVISION_CHANGED', 'EXTRACTION_TIME_LIMIT': 'GRAPH_TIME_LIMIT',
                 'PROCESSING_UNAVAILABLE': 'GRAPH_FAILED'}
        for service_code, expected in cases.items():
            with self.subTest(service_code):
                self.assertEqual(record(ServiceError(service_code, 'x', 503), {}), (expected, [expected]))

    def test_other_errors_are_graph_failed(self):
        self.assertEqual(record(ValueError('bad result'), {}), ('GRAPH_FAILED', ['GRAPH_FAILED']))


class InputChangedTests(unittest.TestCase):
    """KAN-336: 실행 중 새 회신으로 입력이 바뀐 job은 FAILED/GRAPH_FAILED가 아니라 STALE/INPUT_REVISION_CHANGED다.

    운영 사례(11-19 job 4109b7e9): 새 회신이 15:13:18에 revision을 올렸고, 10초마다 도는 임대 갱신이 15:13:20에
    false를 받아(DB lock_case_graph_job은 revision이 달라도 거절한다) 임대 상실로 처리됐다. WORKER_INTERRUPTED는
    임대가 남아 있어 거절됐고 GRAPH_FAILED로 다시 기록됐다."""

    def test_lease_loss_after_input_change_is_stale(self):
        with self.assertLogs('uvicorn.error.case_graph', level='INFO') as logs:
            code, calls = record(OwnershipLost('lost'), {'WORKER_INTERRUPTED': False}, changed=True)
        # WORKER_INTERRUPTED를 먼저 보내지 않는다. 한 번에 INPUT_REVISION_CHANGED로 닫는다
        self.assertEqual((code, calls), ('INPUT_REVISION_CHANGED', ['INPUT_REVISION_CHANGED']))
        self.assertIn('recorded as INPUT_REVISION_CHANGED instead of WORKER_INTERRUPTED job_id=job-1', logs.output[0])

    def test_role_failure_after_input_change_is_stale(self):
        # 역할 안에서 난 REVISION_CONFLICT는 그래프가 일반 오류(RuntimeError·ValueError)로 바꿔 올린다
        for error in (RuntimeError('GRAPH_ROLE_FAILED'), ValueError('Changed graph result scope'),
                      ServiceError('PROCESSING_UNAVAILABLE', 'x', 503), ServiceError('ACTION_NOT_ALLOWED', 'x', 409)):
            with self.subTest(type(error).__name__):
                self.assertEqual(record(error, {}, changed=True),
                                 ('INPUT_REVISION_CHANGED', ['INPUT_REVISION_CHANGED']))

    def test_unchanged_input_keeps_the_failure_code(self):
        self.assertEqual(record(ValueError('bad result'), {}, changed=False), ('GRAPH_FAILED', ['GRAPH_FAILED']))
        self.assertEqual(record(OwnershipLost('lost'), {}, changed=False),
                         ('WORKER_INTERRUPTED', ['WORKER_INTERRUPTED']))

    def test_lookup_failure_keeps_the_failure_code(self):
        # 입력 변경 여부를 확인하지 못하면 추측하지 않는다
        error = ServiceError('PROCESSING_UNAVAILABLE', 'x', 503)
        self.assertEqual(record(ValueError('bad result'), {}, changed=error), ('GRAPH_FAILED', ['GRAPH_FAILED']))
        with self.assertLogs('uvicorn.error.case_graph', level='WARNING'):
            code, calls = record(OwnershipLost('lost'), {'WORKER_INTERRUPTED': False}, changed=error)
        self.assertEqual((code, calls), ('GRAPH_FAILED', ['WORKER_INTERRUPTED', 'GRAPH_FAILED']))

    def test_time_limit_and_shutdown_are_not_relabelled(self):
        # 시간 초과·워커 종료는 입력이 바뀌었더라도 원래 사유가 맞다
        self.assertEqual(record(ServiceError('EXTRACTION_TIME_LIMIT', 'x', 503), {}, changed=True),
                         ('GRAPH_TIME_LIMIT', ['GRAPH_TIME_LIMIT']))
        self.assertEqual(record(ServiceError('WORKER_INTERRUPTED', 'x', 503), {}, changed=True),
                         ('WORKER_INTERRUPTED', ['WORKER_INTERRUPTED']))


class RepositoryInputChangedTests(unittest.TestCase):
    """GraphRuntimeRepository.input_changed: context()가 REVISION_CONFLICT로 보는 조건(revision·batch)과 같다."""

    JOB = {'owner_user_id': 'owner-1', 'case_id': 'case-1', 'input_revision': 2, 'batch_id': 'batch-1'}

    def changed(self, rows):
        from app.features.work_cases.infrastructure.graph_repository import GraphRuntimeRepository

        class Query:
            def __getattr__(self, name):
                return lambda *args, **kwargs: self

            def execute(self):
                return SimpleNamespace(data=rows)

        repo = GraphRuntimeRepository.__new__(GraphRuntimeRepository)
        repo.client = SimpleNamespace(table=lambda name: Query())
        return repo.input_changed(self.JOB)

    def test_same_revision_and_batch_is_unchanged(self):
        self.assertFalse(self.changed([{'input_revision': 2, 'current_batch_id': 'batch-1'}]))

    def test_newer_revision_is_changed(self):
        self.assertTrue(self.changed([{'input_revision': 3, 'current_batch_id': 'batch-1'}]))

    def test_other_batch_is_changed(self):
        self.assertTrue(self.changed([{'input_revision': 2, 'current_batch_id': 'batch-2'}]))

    def test_missing_case_is_not_reported_as_changed(self):
        self.assertFalse(self.changed([]))


class WorkerFlowTests(unittest.TestCase):
    """GraphWorker.process_once 전체 흐름으로 본 KAN-336: 실행 중 새 회신이 revision을 올리면 임대 갱신이 DB에서 거절된다.

    실제 LeaseHeartbeat·ExtractionScope·record_failure를 그대로 쓰고 저장소와 그래프 실행만 가짜다."""

    OWNER, CASE, BATCH, JOB_ID, TOKEN = (str(__import__('uuid').uuid4()) for _ in range(5))
    INITIAL, REPLY = str(__import__('uuid').uuid4()), str(__import__('uuid').uuid4())

    def run_worker(self, *, changed, adapter=None, accepts=None):
        import time
        from functools import partial
        from unittest import mock
        from app.core.leases import LeaseHeartbeat
        from app.features.work_cases.application import graph_worker

        test = self
        calls = []

        class FlowRepo:
            client = None

            def claim(self):
                return {'owner_user_id': test.OWNER, 'case_id': test.CASE, 'input_revision': 2, 'batch_id': test.BATCH,
                        'job_id': test.JOB_ID, 'claim_token': test.TOKEN, 'mail_ids': [test.REPLY],
                        'evidence_mail_ids': [test.INITIAL, test.REPLY], 'execution_mode': 'GRAPH_REVIEW_V1',
                        'start_from': 'REPLY'}

            def context(self, owner, case_id, revision, batch):
                return {'initial_mail_id': test.INITIAL}

            def heartbeat(self, job):
                # 운영 DB heartbeat_case_graph_run은 revision이 바뀌면 false → 저장소가 409로 올린다
                raise ServiceError('EXTRACTION_LEASE_LOST', '그래프 실행 소유권이 변경됐습니다.', 409)

            def input_changed(self, job):
                calls.append('input_changed?')
                return changed

            def fail(self, job, code):
                calls.append(code)
                return (accepts or {}).get(code, True)

        def wait_for_lease_loss(*, settings, scope, runner_factory):
            # ④가 도구를 부르는 동안 임대 갱신이 거절된다. 다음 도구 호출의 한도 확인에서 실행이 멈춘다
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                scope.remaining_seconds()
                time.sleep(0.005)
            raise AssertionError('lease loss was not noticed')

        settings = SimpleNamespace(max_document_characters=1000, agent_graph_review_timeout_seconds=30,
                                   agent_graph_timeout_seconds=30, langsmith_api_key=None)
        worker = GraphWorker(settings, FlowRepo(), mailbox=None, runner_factory=None, attachment_repo=object())
        worker.adapter = adapter or wait_for_lease_loss
        fast = partial(LeaseHeartbeat, grace=60, retry_seconds=0.01)

        def heartbeat(renew, seconds=10, **kwargs):
            return fast(renew, 0.01, **kwargs)       # 10초 주기를 기다리지 않게 주기만 줄인다

        lease_logs = []
        handler = logging.Handler()
        handler.emit = lambda record: lease_logs.append(record.getMessage())
        logging.getLogger('gyeonjwo').addHandler(handler)
        self.addCleanup(logging.getLogger('gyeonjwo').removeHandler, handler)
        with mock.patch.object(graph_worker, 'LeaseHeartbeat', heartbeat), \
                self.assertLogs('uvicorn.error.case_graph', level='INFO') as logs:
            self.assertTrue(worker.process_once())
        return calls, logs.output, lease_logs

    def test_new_reply_during_run_is_recorded_stale(self):
        calls, logs, lease_logs = self.run_worker(changed=True)
        self.assertEqual(calls, ['input_changed?', 'INPUT_REVISION_CHANGED'])
        self.assertTrue(any('lease renewal failed reason=OWNERSHIP_LOST' in line for line in lease_logs))
        self.assertTrue(any('graph failed code=INPUT_REVISION_CHANGED' in line for line in logs))

    def test_real_lease_loss_keeps_the_previous_behaviour(self):
        # 입력은 그대로인데 임대만 잃은 경우(다른 워커가 가져감 등): #187 후속 동작 그대로
        calls, logs, _ = self.run_worker(changed=False, accepts={'WORKER_INTERRUPTED': False})
        self.assertEqual(calls, ['input_changed?', 'WORKER_INTERRUPTED', 'GRAPH_FAILED'])
        self.assertTrue(any('graph failed code=GRAPH_FAILED' in line for line in logs))

    def test_role_failure_after_revision_change_is_recorded_stale(self):
        # 임대 갱신보다 먼저 역할이 실패해 그래프가 일반 오류를 올린 경우
        def failing(*, settings, scope, runner_factory):
            raise RuntimeError('GRAPH_ROLE_FAILED')

        calls, logs, _ = self.run_worker(changed=True, adapter=failing)
        self.assertEqual(calls[-1], 'INPUT_REVISION_CHANGED')
        self.assertNotIn('GRAPH_FAILED', calls)


if __name__ == '__main__':
    unittest.main()
