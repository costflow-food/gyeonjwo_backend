import logging
import threading
import time

from app.core.errors import ServiceError
from app.shared.infrastructure.db.contracts import OwnershipLost

logger = logging.getLogger('gyeonjwo')

# DB가 작업을 넘겨줄 때 잡는 임대 시간(claim/heartbeat의 interval '120 seconds').
LEASE_SECONDS = 120


def definite_loss(error):
    """DB가 "이 토큰의 임대는 끝났다"고 답한 경우만 True. 연결 끊김·시간 초과 같은 일시 오류는 False다.
    repository의 heartbeat는 소유권이 넘어가면 409(EXTRACTION_LEASE_LOST·SYNC_OWNERSHIP_LOST)를,
    DB에 닿지 못하면 503(PROCESSING_UNAVAILABLE)을 올린다."""
    if isinstance(error, OwnershipLost):
        return True
    return isinstance(error, ServiceError) and error.status == 409


class LeaseHeartbeat:
    """작업 임대를 `seconds`마다 갱신한다.

    한 번의 일시 오류로 소유권을 포기하지 않는다. DB가 소유권이 넘어갔다고 답하면 바로 상실로 보고,
    그 밖의 실패는 `retry_seconds` 간격으로 다시 시도한다. 마지막 성공 갱신 뒤 `grace`초가 지나도
    갱신하지 못하면 그때 상실로 본다. `grace`는 DB 임대 시간보다 짧아야 다른 워커가 같은 작업을 가져가기
    전에 멈춘다(기본: 임대 시간의 절반)."""

    def __init__(self, renew, seconds=30, *, grace=None, retry_seconds=2, clock=time.monotonic):
        self.renew, self.seconds = renew, seconds
        self.grace = LEASE_SECONDS / 2 if grace is None else grace
        self.retry_seconds = retry_seconds
        self.clock = clock
        self.stop = threading.Event()
        self.lost = threading.Event()
        self.thread = None

    def __enter__(self):
        def loop():
            last_renewed = self.clock()
            wait = self.seconds
            while not self.stop.wait(wait):
                try:
                    self.renew()
                except Exception as error:
                    kind = type(error).__name__
                    code = getattr(error, 'code', None) if isinstance(error, ServiceError) else None
                    if definite_loss(error):
                        self.lost.set()
                        logger.warning('lease renewal failed reason=OWNERSHIP_LOST error=%s code=%s', kind, code)
                        return
                    elapsed = self.clock() - last_renewed
                    if elapsed >= self.grace:
                        self.lost.set()
                        logger.warning('lease renewal failed reason=RETRY_EXHAUSTED error=%s code=%s since_ok_s=%.1f',
                                       kind, code, elapsed)
                        return
                    logger.warning('lease renewal retry error=%s code=%s since_ok_s=%.1f', kind, code, elapsed)
                    wait = self.retry_seconds
                    continue
                last_renewed = self.clock()
                wait = self.seconds
        self.thread = threading.Thread(target=loop, daemon=True)
        self.thread.start()
        return self

    def check(self):
        if self.lost.is_set():
            raise OwnershipLost('Lease ownership could not be renewed')

    def __exit__(self, *args):
        self.stop.set()
        self.thread.join(timeout=20)
