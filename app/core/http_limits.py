from starlette.responses import JSONResponse

from app.core.errors import error_payload

MAX_BODY_BYTES = 65536


def _too_large():
    return JSONResponse(error_payload('REQUEST_TOO_LARGE', '요청 본문이 너무 큽니다.', 413), status_code=413)


class BodyLimitMiddleware:
    """Single place for request body limits: rejects a declared oversize Content-Length early and
    bounds chunked bodies before FastAPI parses them (Content-Length is not trusted alone)."""
    def __init__(self, app, limit=MAX_BODY_BYTES):
        self.app, self.limit = app, limit

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http' or scope['method'] not in ('POST', 'PATCH', 'PUT'):
            return await self.app(scope, receive, send)
        declared = dict(scope.get('headers') or []).get(b'content-length', b'')
        if declared.isdigit() and int(declared) > self.limit:
            return await _too_large()(scope, receive, send)
        chunks, size = [], 0
        while True:
            message = await receive()
            if message['type'] == 'http.disconnect':
                return
            size += len(message.get('body', b''))
            if size > self.limit:
                return await _too_large()(scope, receive, send)
            chunks.append(message.get('body', b''))
            if not message.get('more_body', False):
                break
        delivered = False
        async def replay():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {'type': 'http.request', 'body': b''.join(chunks), 'more_body': False}
            return await receive()
        await self.app(scope, replay, send)
