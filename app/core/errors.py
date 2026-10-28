from app.core.request_context import current_request_id

# 08 오류 계약: 5xx·429는 기본적으로 재시도 가능, 409 상태 충돌은 최신 재조회를 안내한다.
RETRYABLE_STATUSES = frozenset({429, 502, 503, 504})
DEFAULT_NEXT_ACTIONS = {
    'REVISION_CONFLICT': 'REFRESH_CASE',
    'ACTION_NOT_ALLOWED': 'REFRESH_CASE',
    'WORK_ALREADY_STARTED': 'REFRESH_CASE',
}


class ServiceError(Exception):
    def __init__(self, code: str, message: str, status: int = 503, *, fields: list[str] | None = None,
                 current_revision: int | None = None, retryable: bool | None = None, next_action: str | None = None):
        self.code, self.message, self.status = code, message, status
        self.fields, self.current_revision = list(fields or []), current_revision
        self.retryable, self.next_action = retryable, next_action
        super().__init__(code)


class ReviewRequired(Exception):
    def __init__(self, code: str, message: str, source: str = ''):
        self.issue = {'code': code, 'message': message, 'source_location': source}
        super().__init__(code)


def error_payload(code: str, message: str, status: int, *, fields: list[str] | None = None,
                  current_revision: int | None = None, retryable: bool | None = None,
                  next_action: str | None = None) -> dict:
    """Body of every error response: {"error": {...}, "meta": {"request_id"}}."""
    return {
        'error': {
            'code': code,
            'message': message,
            'fields': list(fields or []),
            'current_revision': current_revision,
            'retryable': status in RETRYABLE_STATUSES if retryable is None else retryable,
            'next_action': next_action if next_action is not None else DEFAULT_NEXT_ACTIONS.get(code),
        },
        'meta': {'request_id': current_request_id()},
    }
