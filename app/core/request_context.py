"""Per-request identifier shared by success/error envelopes, response headers and logs (08 공통 계약 meta.request_id)."""
from contextvars import ContextVar
from uuid import uuid4

_request_id: ContextVar[str | None] = ContextVar('request_id', default=None)


def new_request_id() -> str:
    """Start a new request scope. The server always generates the ID; client-supplied values are not trusted."""
    value = 'req-' + uuid4().hex
    _request_id.set(value)
    return value


def current_request_id() -> str:
    value = _request_id.get()
    return value if value else new_request_id()
