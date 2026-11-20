"""조회(GET)가 일시 오류로 실패하면 잠깐 쉬고 다시 읽는다 (KAN-339).

운영 사례(11-20 업무 c1eaf264): ④ 구매안 검증 도구(validate_purchase_plan)의 조회 한 번이 실패해 역할이 끝났고
업무 전체가 GRAPH_FAILED가 됐다. 같은 시각 Supabase에는 5xx 응답이 없었다. 요청이 DB에 닿기 전의 연결 오류다.
조회는 여러 번 해도 결과가 같으므로 다시 읽어도 안전하다. 저장(RPC)에는 쓰지 않는다. 응답을 받지 못한 저장은
저장됐는지 알 수 없다.
"""
import logging
import time

import httpx

logger = logging.getLogger('gyeonjwo')

# 처음 한 번 + 다시 두 번. 쉬는 시간은 끊긴 연결을 풀이 버리고 새로 맺을 만큼만 둔다
READ_RETRY_PAUSES = (0.3, 1.0)
# DB·PostgREST가 "지금은 처리하지 못했다"고 답한 코드. 그 밖의 응답(문법·권한·없는 열)은 다시 읽어도 같다
#   57014 statement timeout·취소, 55P03 lock timeout, 40P01 deadlock, 53300 연결 수 초과,
#   PGRST000~003 PostgREST가 DB에 연결하지 못함
TRANSIENT_CODES = frozenset({'57014', '55P03', '40P01', '53300', 'PGRST000', 'PGRST001', 'PGRST002', 'PGRST003'})


def transient(error) -> bool:
    """연결 끊김·시간 초과(httpx 전송 오류)나 DB가 일시적으로 처리하지 못한 응답인지."""
    return isinstance(error, httpx.TransportError) or getattr(error, 'code', None) in TRANSIENT_CODES


def read_with_retry(read, *, before_attempt=None, label='read', sleep=None):
    """read()를 부르고, 일시 오류면 READ_RETRY_PAUSES만큼 쉬고 다시 부른다. 마지막 오류는 그대로 올린다.
    before_attempt: 매 시도 전에 부를 확인(실행 시간 한도·임대). 여기서 난 예외는 다시 시도하지 않는다."""
    for attempt in range(len(READ_RETRY_PAUSES) + 1):
        if before_attempt is not None:
            before_attempt()
        try:
            return read()
        except Exception as error:
            if attempt == len(READ_RETRY_PAUSES) or not transient(error):
                raise
            # 원문·질의는 남기지 않는다. 예외 종류와 코드형 값만 남긴다
            logger.warning('db read retry label=%s attempt=%s error=%s code=%s', label, attempt + 1,
                           type(error).__name__, getattr(error, 'code', None))
            (sleep or time.sleep)(READ_RETRY_PAUSES[attempt])
