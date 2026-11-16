from concurrent.futures import ThreadPoolExecutor
from threading import Event
from unittest import TestCase
from unittest.mock import Mock, patch

from app.shared.infrastructure.db.health_probe import HealthProbe


class HealthProbeTests(TestCase):
    def test_concurrent_timeouts_share_refresh_and_cache_success(self):
        gate = Event()
        started = Event()

        def refresh():
            started.set()
            if not gate.wait(5):
                raise RuntimeError('Test did not release the probe')
            return True

        check = Mock(side_effect=refresh)
        probe = HealthProbe(check, bool)
        try:
            with ThreadPoolExecutor(max_workers=12) as callers:
                results = list(callers.map(lambda _: probe.read(timeout=0.05), range(12)))
            self.assertTrue(started.wait(1))
            self.assertEqual(results, [None] * 12)
            check.assert_called_once()
        finally:
            gate.set()
        self.assertIs(probe.read(timeout=1), True)
        self.assertIs(probe.read(timeout=0), True)
        check.assert_called_once()

    def test_failure_expires_and_probe_recovers(self):
        check = Mock(side_effect=[RuntimeError('Unavailable'), True])
        probe = HealthProbe(check, bool)
        with patch('app.shared.infrastructure.db.health_probe.monotonic', return_value=100) as clock:
            self.assertIsNone(probe.read(timeout=1))
            clock.return_value = 104
            self.assertIsNone(probe.read(timeout=1))
            check.assert_called_once()
            clock.return_value = 105
            self.assertIs(probe.read(timeout=1), True)
        self.assertEqual(check.call_count, 2)


# ── KAN-273 · KAN-267: TTL 경계·readiness 상태·복구 경로 캐시 공유 (가짜 시계, 실제 sleep 없음) ──
CLOCK = 'app.shared.infrastructure.db.health_probe.monotonic'


class HealthProbeTtlTests(TestCase):
    def test_success_ttl_boundary(self):
        check = Mock(return_value=True)
        probe = HealthProbe(check, bool)
        with patch(CLOCK, return_value=100) as clock:
            probe.read(timeout=1)
            clock.return_value = 159.999
            probe.read(timeout=1)
            self.assertEqual(check.call_count, 1)
            clock.return_value = 160
            probe.read(timeout=1)
        self.assertEqual(check.call_count, 2)

    def test_unhealthy_value_is_cached_only_briefly(self):
        check = Mock(side_effect=[False, True])
        probe = HealthProbe(check, bool)
        with patch(CLOCK, return_value=100) as clock:
            self.assertIs(probe.read(timeout=1), False)
            clock.return_value = 104.999
            self.assertIs(probe.read(timeout=1), False)
            clock.return_value = 105
            self.assertIs(probe.read(timeout=1), True)
        self.assertEqual(check.call_count, 2)


class ReadinessTests(TestCase):
    def test_fresh_success_and_failure(self):
        with patch(CLOCK, return_value=100):
            self.assertEqual(HealthProbe(Mock(return_value='v1'), bool).readiness(), ('ready', 'v1'))
            self.assertEqual(HealthProbe(Mock(side_effect=RuntimeError('down')), bool).readiness(), ('failed', None))
            self.assertEqual(HealthProbe(Mock(return_value='old'), lambda v: v == 'new').readiness(), ('failed', 'old'))

    def test_stale_success_during_slow_refresh_is_refreshing_then_ready(self):
        gate, started = Event(), Event()
        values = iter(['v1', 'v2'])

        def check():
            value = next(values)
            if value == 'v2':
                started.set()
                gate.wait(5)
            return value

        probe = HealthProbe(Mock(side_effect=check), bool)
        with patch(CLOCK, return_value=100) as clock:
            self.assertEqual(probe.readiness(), ('ready', 'v1'))
            clock.return_value = 162  # 성공 TTL(60) 만료 후 유예(5) 안
            try:
                self.assertEqual(probe.readiness(), ('refreshing', 'v1'))  # 기다리지 않고 직전 성공값
                self.assertTrue(started.wait(1))
                self.assertEqual(probe.readiness(), ('refreshing', 'v1'))  # 갱신은 하나만
            finally:
                gate.set()
            self.assertEqual(probe.read(timeout=1), 'v2')
            self.assertEqual(probe.readiness(), ('ready', 'v2'))

    def test_slow_refresh_without_recent_success_is_checking(self):
        gate = Event()
        check = Mock(side_effect=lambda: gate.wait(5))
        probe = HealthProbe(check, bool)
        try:
            # 성공 이력이 없으면 'ready'로 속이지 않는다. 대기는 0.75초로 묶여 있다
            self.assertEqual(probe.readiness(), ('checking', None))
        finally:
            gate.set()
        check.assert_called_once()

    def test_failure_after_success_is_not_ready_once_grace_ends(self):
        probe = HealthProbe(Mock(side_effect=['v1', RuntimeError('down')]), bool)
        with patch(CLOCK, return_value=100) as clock:
            self.assertEqual(probe.readiness(), ('ready', 'v1'))
            clock.return_value = 170  # 유예 이후
            self.assertEqual(probe.readiness(), ('failed', None))


class GraphRecoverCacheTests(TestCase):
    """recover가 약 10초마다 돌아도 정밀 검사 case_graph_health는 캐시를 공유한다."""

    def repo(self, health):
        from app.features.work_cases.infrastructure.graph_repository import GraphRuntimeRepository
        calls = []
        results = {'recover_case_graph_runs': 0, 'schedule_case_graph_continuations': 0, 'case_graph_health': health}

        def rpc(name, params):
            calls.append(name)
            return Mock(execute=Mock(return_value=Mock(data=results.get(name, 0))))

        return GraphRuntimeRepository(Mock(rpc=Mock(side_effect=rpc))), calls

    def test_repeated_recover_and_readers_share_one_health_rpc(self):
        repo, calls = self.repo({'contract_version': 1, 'ready': True, 'legacy_queue_drained': True,
                                 'capabilities': ['START', 'SCOPED_LEASES', 'DOCUMENTS', 'CANDIDATES', 'DRAFTS', 'RUNS',
                                                  'RESULTS', 'LEGACY_RFQ_GUARD', 'CAUTION_GUARD', 'CANDIDATE_SETS',
                                                  'USER_SELECTION_GATE', 'EXTRACTED_CONTINUATIONS']})
        self.assertTrue(repo.health_ready(repo.client.rpc('case_graph_health', {}).execute().data))
        calls.clear()
        with patch(CLOCK, return_value=100) as clock:
            for second in range(0, 60, 10):
                clock.return_value = 100 + second
                repo.recover()
                repo.read_health(timeout=0.05)
                repo.available()
            self.assertEqual(calls.count('case_graph_health'), 1)
            clock.return_value = 160
            repo.recover()
        self.assertEqual(calls.count('case_graph_health'), 2)
        self.assertEqual(calls.count('recover_case_graph_runs'), 7)  # 복구 자체는 매번 한다

    def test_rpc_failure_is_rechecked_after_failure_ttl(self):
        repo, calls = self.repo(None)
        with patch(CLOCK, return_value=100) as clock:
            self.assertFalse(repo.available())
            clock.return_value = 104
            self.assertFalse(repo.available())
            self.assertEqual(calls.count('case_graph_health'), 1)
            clock.return_value = 105
            repo.available()
        self.assertEqual(calls.count('case_graph_health'), 2)
