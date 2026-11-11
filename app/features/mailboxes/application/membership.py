"""허용 사용자 확인 (KAN-203).

Google 신원 확인(google_principal) 바로 뒤에 DB `public.check_app_member`로 허용 목록을 확인한다.
- 허용: user_id별 60초 메모리 캐시. 거부·오류는 캐시하지 않는다(새로 등록한 사람이 바로 쓰게).
- 거부: 403 ACCESS_DENIED. 거부 사유(reason)는 로그에만 남기고 응답에는 넣지 않는다.
- 입력 오류(22023): 401 GOOGLE_RECONNECT_REQUIRED.
- 그 밖의 DB 오류·이상한 응답: 503 AUTH_UNAVAILABLE. 어떤 경우에도 오류를 허용으로 처리하지 않는다.
처음 로그인한 사용자는 이 호출에서 DB가 user_id를 연결한다(쓰기). 서버 키 클라이언트로만 부른다.
"""
import logging
import threading
import time

from app.core.errors import ServiceError

logger = logging.getLogger('gyeonjwo')
ALLOW_CACHE_SECONDS = 60
_REASONS = frozenset({'MEMBER', 'NOT_MEMBER', 'DISABLED', 'EMAIL_LINKED_TO_OTHER_USER'})


def _unavailable():
    return ServiceError('AUTH_UNAVAILABLE', '사용 권한을 확인할 수 없습니다. 잠시 후 다시 시도해 주세요.', 503)


class MembershipChecker:
    def __init__(self, client, *, clock=time.monotonic, ttl=ALLOW_CACHE_SECONDS):
        self.client, self.clock, self.ttl = client, clock, ttl
        self._allowed_until: dict[str, float] = {}
        self._lock = threading.Lock()

    def require(self, principal) -> None:
        now = self.clock()
        with self._lock:
            if self._allowed_until.get(principal.user_id, 0) > now:
                return
            self._allowed_until.pop(principal.user_id, None)
        try:
            result = self.client.rpc('check_app_member', {'p_user': principal.user_id,
                                                          'p_email': principal.email}).execute().data
        except Exception as error:
            code = getattr(error, 'code', None)
            if code == '22023':
                raise ServiceError('GOOGLE_RECONNECT_REQUIRED', 'Google 계정으로 다시 로그인해 주세요.', 401) from None
            # 코드만 남긴다(메시지에 입력값이 섞일 수 있음).
            logger.warning('member check unavailable code=%s', code if isinstance(code, str) else type(error).__name__)
            raise _unavailable() from None
        if not isinstance(result, dict) or type(result.get('allowed')) is not bool:
            logger.warning('member check unavailable code=INVALID_RESPONSE')
            raise _unavailable()
        if not result['allowed']:
            reason = result.get('reason') if result.get('reason') in _REASONS else 'UNKNOWN'
            logger.info('member access denied reason=%s user_id=%s', reason, principal.user_id)
            raise ServiceError('ACCESS_DENIED', '이 서비스를 사용할 권한이 없습니다. 관리자에게 문의해 주세요.', 403,
                               retryable=False)
        with self._lock:
            self._allowed_until[principal.user_id] = self.clock() + self.ttl
