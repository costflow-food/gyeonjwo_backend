import re

from app.core.request_context import current_request_id

# 실패 원인으로 저장해도 되는 짧은 코드(예외 클래스·오류 코드·SQLSTATE·도구 이름). 메일 원문·자유 문장은 이 모양이 아니다
CODE_TOKEN = re.compile(r'[A-Za-z0-9][A-Za-z0-9_]{0,63}')
# DB가 RAISE EXCEPTION 'REVIEW_SUPPLIER_NOT_AUTHORIZED'처럼 코드로 올리는 메시지. 문장형 메시지는 남기지 않는다
DB_CODE = re.compile(r'[A-Z][A-Z0-9_]{2,63}')
SQLSTATE = re.compile(r'[0-9A-Z]{5}')

# 08 오류 계약: 5xx·429는 기본적으로 재시도 가능, 409 상태 충돌은 최신 재조회를 안내한다.
RETRYABLE_STATUSES = frozenset({429, 502, 503, 504})
DEFAULT_NEXT_ACTIONS = {
    'REVISION_CONFLICT': 'REFRESH_CASE',
    'ACTION_NOT_ALLOWED': 'REFRESH_CASE',
    'WORK_ALREADY_STARTED': 'REFRESH_CASE',
}


class ServiceError(Exception):
    def __init__(self, code: str, message: str, status: int = 503, *, fields: list[str] | None = None,
                 current_revision: int | None = None, retryable: bool | None = None, next_action: str | None = None,
                 cause: dict | None = None):
        self.code, self.message, self.status = code, message, status
        self.fields, self.current_revision = list(fields or []), current_revision
        self.retryable, self.next_action = retryable, next_action
        # 원인 기록용(응답에는 넣지 않는다): 감싼 DB 오류의 SQLSTATE·코드형 메시지 {db_sqlstate, db_code}
        self.cause = dict(cause or {})
        super().__init__(code)


def code_token(value) -> str | None:
    """실패 원인에 남길 수 있는 짧은 코드면 그대로, 아니면 None."""
    return value if isinstance(value, str) and CODE_TOKEN.fullmatch(value) else None


def db_error_cause(error) -> dict:
    """감싼 DB(PostgREST) 오류에서 남겨도 되는 것만: SQLSTATE와 코드형 메시지. 문장·값은 버린다."""
    cause = {}
    state, message = getattr(error, 'code', None), getattr(error, 'message', None)
    if isinstance(state, str) and SQLSTATE.fullmatch(state):
        cause['db_sqlstate'] = state
    if isinstance(message, str) and DB_CODE.fullmatch(message):
        cause['db_code'] = message
    return cause


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
