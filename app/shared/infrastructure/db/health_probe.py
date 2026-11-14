"""Single-flight contract checks with bounded waits and short failure caching."""
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from threading import Lock
from time import monotonic

_POOL = ThreadPoolExecutor(max_workers=6, thread_name_prefix='db-health')


class HealthProbe:
    def __init__(self, check, healthy):
        self._check = check
        self._healthy = healthy
        self._lock = Lock()
        self._pending = None
        self._expires = 0
        self._value = None
        self._healthy_value = False

    def read(self, timeout=None):
        with self._lock:
            if monotonic() < self._expires:
                return self._value
            if self._pending is None:
                self._pending = _POOL.submit(self._refresh)
            pending = self._pending
        try:
            return pending.result(timeout=timeout)
        except TimeoutError:
            return None

    def readiness(self):
        """Allow brief stale success while a refresh is in progress."""
        with self._lock:
            now = monotonic()
            if now < self._expires:
                return ('ready' if self._healthy_value else 'failed'), self._value
            if self._pending is None:
                self._pending = _POOL.submit(self._refresh)
            pending = self._pending
            if self._healthy_value and now < self._expires + 5:
                return 'refreshing', self._value
        try:
            pending.result(timeout=0.75)
        except TimeoutError:
            return 'checking', None
        with self._lock:
            return ('ready' if self._healthy_value else 'failed'), self._value

    def _refresh(self):
        try:
            value = self._check()
            healthy = self._healthy(value)
        except Exception:
            value, healthy = None, False
        with self._lock:
            self._value = value
            self._healthy_value = healthy
            self._expires = monotonic() + (60 if healthy else 5)
            self._pending = None
        return value
