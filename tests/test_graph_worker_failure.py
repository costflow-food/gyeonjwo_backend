"""그래프 job 실패 기록 (#187 후속).

#187은 임대를 잃은 실행을 WORKER_INTERRUPTED로 기록한다. 그런데 운영 DB fail_case_graph_run은 임대가 아직 남아 있으면
WORKER_INTERRUPTED를 받지 않고 false를 돌려준다. 상실을 판단한 시점(갱신 실패 60초·DB가 소유권 변경을 알림)에는 임대가
대개 남아 있어, job이 recover_case_graph_runs가 정리할 때까지 최대 약 2분 PROCESSING으로 남았다.
"""
import unittest
from types import SimpleNamespace

from app.core.errors import ServiceError
from app.features.work_cases.application.graph_worker import GraphWorker
from app.shared.infrastructure.db.contracts import OwnershipLost

JOB = {'job_id': 'job-1', 'claim_token': 'token-1'}


class Repo:
    """fail_case_graph_run 가짜. 코드별 반환값을 정해 두고 호출을 기록한다."""

    def __init__(self, accepts):
        self.accepts, self.calls = accepts, []

    def fail(self, job, code):
        self.calls.append(code)
        return self.accepts.get(code, True)


def record(error, accepts):
    repo = Repo(accepts)
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


if __name__ == '__main__':
    unittest.main()
