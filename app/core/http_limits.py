from starlette.responses import JSONResponse


class BodyLimitMiddleware:
    """Bound chunked JSON requests before FastAPI parses them; no Content-Length trust."""
    def __init__(self, app, limit=65536):
        self.app, self.limit = app, limit

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http' or scope['method'] not in ('POST', 'PATCH', 'PUT'):
            return await self.app(scope, receive, send)
        chunks, size = [], 0
        while True:
            message = await receive()
            if message['type'] == 'http.disconnect':
                return
            size += len(message.get('body', b''))
            if size > self.limit:
                response = JSONResponse({'error': {'code': 'REQUEST_TOO_LARGE', 'message': '요청 본문이 너무 큽니다.', 'details': {}}},
                                        status_code=413, headers={'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff'})
                return await response(scope, receive, send)
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
