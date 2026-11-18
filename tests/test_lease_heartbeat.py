import threading
import time
import unittest

from app.core.errors import ServiceError
from app.core.leases import LeaseHeartbeat
from app.shared.infrastructure.db.contracts import OwnershipLost


def transient():
    return ServiceError('PROCESSING_UNAVAILABLE', '업무 실행 저장소를 확인할 수 없습니다.', 503)


class Renewals:
    """renew()가 순서대로 실패·성공하게 한다. 목록이 끝나면 계속 성공한다."""

    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = 0
        self.done = threading.Event()

    def __call__(self):
        self.calls += 1
        if not self.outcomes:
            self.done.set()
            return True
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def wait_until(predicate, timeout=2.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


class LeaseHeartbeatTests(unittest.TestCase):
    def test_transient_failure_is_retried_and_does_not_lose_the_lease(self):
        # 11-18 망고 f40a00e6: 연결이 한 번 끊겨 갱신이 실패했고, 그 한 번으로 실행 전체가 멈췄다
        renew = Renewals([transient(), transient()])
        with self.assertLogs('gyeonjwo', level='WARNING') as logs:
            with LeaseHeartbeat(renew, seconds=0.02, retry_seconds=0.01, grace=1.0) as lease:
                self.assertTrue(renew.done.wait(2.0))
                lease.check()
        self.assertFalse(lease.lost.is_set())
        self.assertGreaterEqual(renew.calls, 3)
        self.assertTrue(all('lease renewal retry' in line for line in logs.output))

    def test_ownership_conflict_loses_the_lease_at_once(self):
        renew = Renewals([ServiceError('EXTRACTION_LEASE_LOST', '그래프 실행 소유권이 변경됐습니다.', 409)])
        with self.assertLogs('gyeonjwo', level='WARNING') as logs:
            with LeaseHeartbeat(renew, seconds=0.01, retry_seconds=0.01, grace=1.0) as lease:
                self.assertTrue(wait_until(lease.lost.is_set))
                with self.assertRaises(OwnershipLost):
                    lease.check()
                time.sleep(0.05)
        self.assertEqual(renew.calls, 1)
        self.assertIn('reason=OWNERSHIP_LOST', logs.output[-1])
        self.assertIn('code=EXTRACTION_LEASE_LOST', logs.output[-1])

    def test_ownership_lost_error_loses_the_lease_at_once(self):
        renew = Renewals([OwnershipLost('stale token')])
        with LeaseHeartbeat(renew, seconds=0.01, retry_seconds=0.01, grace=1.0) as lease:
            self.assertTrue(wait_until(lease.lost.is_set))
            time.sleep(0.05)
        self.assertEqual(renew.calls, 1)

    def test_failures_past_the_grace_period_lose_the_lease(self):
        renew = Renewals([transient()] * 1000)
        with self.assertLogs('gyeonjwo', level='WARNING') as logs:
            with LeaseHeartbeat(renew, seconds=0.01, retry_seconds=0.01, grace=0.1) as lease:
                self.assertTrue(wait_until(lease.lost.is_set))
                with self.assertRaises(OwnershipLost):
                    lease.check()
        self.assertGreater(renew.calls, 1)
        self.assertIn('reason=RETRY_EXHAUSTED', logs.output[-1])

    def test_a_success_restarts_the_grace_period(self):
        # 실패 → 성공 → 실패가 반복돼도, 성공할 때마다 시간을 다시 재므로 잃지 않는다
        clock = [0.0]
        renew = Renewals([transient(), True, transient(), True, transient()])
        hb = LeaseHeartbeat(renew, seconds=0.01, retry_seconds=0.01, grace=5.0, clock=lambda: clock[0])

        def tick():
            clock[0] += 3.0

        original = renew.__call__

        class Ticking:
            def __call__(self_inner):
                tick()
                return original()

        hb.renew = Ticking()
        with self.assertLogs('gyeonjwo', level='WARNING'):
            with hb as lease:
                self.assertTrue(renew.done.wait(2.0))
                lease.check()
        self.assertFalse(lease.lost.is_set())

    def test_default_grace_is_half_the_db_lease(self):
        self.assertEqual(LeaseHeartbeat(lambda: True).grace, 60)


if __name__ == '__main__':
    unittest.main()
