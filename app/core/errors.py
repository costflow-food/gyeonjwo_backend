class ServiceError(Exception):
    def __init__(self, code: str, message: str, status: int = 503, details: dict | None = None):
        self.code, self.message, self.status, self.details = code, message, status, details or {}
        super().__init__(code)


class ReviewRequired(Exception):
    def __init__(self, code: str, message: str, source: str = ''):
        self.issue = {'code': code, 'message': message, 'source_location': source}
        super().__init__(code)
