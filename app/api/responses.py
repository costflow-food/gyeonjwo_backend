"""08 공통 응답 계약: 성공은 {"data", "meta"}, 오류는 {"error", "meta"}."""
import json
from typing import Generic, TypeVar

from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.core.errors import ServiceError, error_payload
from app.core.request_context import current_request_id
from app.shared.infrastructure.llm import json_default

T = TypeVar('T')


class Meta(BaseModel):
    request_id: str


class Envelope(BaseModel, Generic[T]):
    data: T
    meta: Meta


class ErrorDetail(BaseModel):
    code: str
    message: str
    fields: list[str]
    current_revision: int | None
    retryable: bool
    next_action: str | None


class ErrorEnvelope(BaseModel):
    error: ErrorDetail
    meta: Meta


def api_json(data, status=200, model=None, headers=None):
    """Serialize a successful result inside the 08 envelope. `model` validates the data part only."""
    data = json.loads(json.dumps(data, default=json_default))
    if model is not None:
        data = model.model_validate(data).model_dump(mode='json')
    return JSONResponse({'data': data, 'meta': {'request_id': current_request_id()}}, status_code=status, headers=headers)


def api_error(code, message, status, headers=None, **contract):
    return JSONResponse(error_payload(code, message, status, **contract), status_code=status, headers=headers)


def service_error_response(error: ServiceError):
    return api_error(error.code, error.message, error.status, fields=error.fields, current_revision=error.current_revision,
                     retryable=error.retryable, next_action=error.next_action)
