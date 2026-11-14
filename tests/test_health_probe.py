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
