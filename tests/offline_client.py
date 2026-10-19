"""Optional no-I/O asyncio loop for ASGI tests in socket-restricted environments.

The app uses an in-memory Supabase fake. A thread Event supplies loop wakeups;
no network/file-descriptor operations are supported. Production never imports this.
Ordinary local/CI runs use the standard asyncio loop.
"""

import asyncio
import os
import selectors
from threading import Event

from fastapi.testclient import TestClient


class MemorySelector(selectors.BaseSelector):
    def __init__(self):
        self.wakeup = Event()

    def register(self, fileobj, events, data=None):
        raise RuntimeError("Network/file-descriptor I/O is forbidden in offline ASGI tests")

    def unregister(self, fileobj):
        raise KeyError(fileobj)

    def select(self, timeout=None):
        self.wakeup.wait(timeout)
        self.wakeup.clear()
        return []

    def get_map(self):
        return {}

    def close(self):
        self.wakeup.set()


class MemoryLoop(asyncio.SelectorEventLoop):
    def __init__(self):
        super().__init__(MemorySelector())

    def _make_self_pipe(self):
        pass

    def _close_self_pipe(self):
        pass

    def _write_to_self(self):
        if self._selector is not None:
            self._selector.wakeup.set()


def client_for(app):
    if os.environ.get("COSTFLOW_TEST_NO_SOCKETS") == "1":
        return TestClient(app, backend_options={"loop_factory": MemoryLoop})
    return TestClient(app)
