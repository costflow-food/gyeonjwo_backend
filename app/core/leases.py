import logging
import threading

from app.shared.infrastructure.db.contracts import OwnershipLost

logger = logging.getLogger('gyeonjwo')


class LeaseHeartbeat:
    def __init__(self, renew, seconds=30):
        self.renew, self.seconds = renew, seconds
        self.stop = threading.Event()
        self.lost = threading.Event()
        self.thread = None

    def __enter__(self):
        def loop():
            while not self.stop.wait(self.seconds):
                try:
                    self.renew()
                except Exception:
                    self.lost.set()
                    logger.warning('lease renewal failed')
                    return
        self.thread = threading.Thread(target=loop, daemon=True)
        self.thread.start()
        return self

    def check(self):
        if self.lost.is_set():
            raise OwnershipLost('Lease ownership could not be renewed')

    def __exit__(self, *args):
        self.stop.set()
        self.thread.join(timeout=20)
