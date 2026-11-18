import logging
import threading
import time

from app.core.errors import ServiceError, code_token
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


def error_fields(error) -> str:
    """갱신 실패 로그에 남길 코드형 값. 저장소는 연결 끊김·DB 오류를 모두 ServiceError로 감싸므로(`from None`)
    감싸기 전 예외 종류(__context__)와 DB SQLSTATE를 함께 남겨 둘을 구분한다. 메시지 원문은 남기지 않는다."""
    inner = getattr(error, '__context__', None)
    cause = getattr(error, 'cause', None) if isinstance(error, ServiceError) else None
    return 'error=%s code=%s cause=%s sqlstate=%s' % (
        code_token(type(error).__name__), code_token(getattr(error, 'code', None)) if isinstance(error, ServiceError) else None,
        code_token(type(inner).__name__) if inner is not None else None,
        code_token((cause or {}).get('db_sqlstate')) if isinstance(cause, dict) else None)


class LeaseHeartbeat:
    """작업 임대를 `seconds`마다 갱신한다.

    한 번의 일시 오류로 소유권을 포기하지 않는다. DB가 소유권이 넘어갔다고 답하면 바로 상실로 보고,
    그 밖의 실패는 `retry_seconds` 간격으로 다시 시도한다. 마지막 성공 갱신 뒤 `grace`초가 지나도
    갱신하지 못하면 그때 상실로 본다. `grace`는 DB 임대 시간보다 짧아야 다른 워커가 같은 작업을 가져가기
    전에 멈춘다(기본: 임대 시간의 절반).
    `worker`·`job_id`는 로그에서 어느 워커의 어느 작업인지 잇기 위한 고정 라벨·ID다(원문 없음)."""

    def __init__(self, renew, seconds=30, *, grace=None, retry_seconds=2, clock=time.monotonic,
                 worker=None, job_id=None):
        self.renew, self.seconds = renew, seconds
        self.grace = LEASE_SECONDS / 2 if grace is None else grace
        self.retry_seconds = retry_seconds
        self.clock = clock
        self.worker, self.job_id = code_token(worker), job_id
        self.stop = threading.Event()
        self.lost = threading.Event()
        self.thread = None

    def __enter__(self):
        where = 'worker=%s job_id=%s' % (self.worker, self.job_id)

        def loop():
            last_renewed = self.clock()
            wait = self.seconds
            while not self.stop.wait(wait):
                try:
                    self.renew()
                except Exception as error:
                    fields = error_fields(error)
                    if definite_loss(error):
                        self.lost.set()
                        logger.warning('lease renewal failed reason=OWNERSHIP_LOST %s %s', where, fields)
                        return
                    elapsed = self.clock() - last_renewed
                    if elapsed >= self.grace:
                        self.lost.set()
                        logger.warning('lease renewal failed reason=RETRY_EXHAUSTED %s %s since_ok_s=%.1f',
                                       where, fields, elapsed)
                        return
                    logger.warning('lease renewal retry %s %s since_ok_s=%.1f', where, fields, elapsed)
                    wait = self.retry_seconds
                    continue
                last_renewed = self.clock()
                wait = self.seconds
        self.thread = threading.Thread(target=loop, daemon=True, name=f'lease-{self.worker or "job"}')
        self.thread.start()
        return self

    def check(self):
        if self.lost.is_set():
            raise OwnershipLost('Lease ownership could not be renewed')

    def __exit__(self, *args):
        self.stop.set()
        self.thread.join(timeout=20)
