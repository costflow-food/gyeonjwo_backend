"""Safe, provider-neutral failures. Never put provider error bodies in messages."""


class ProviderFailure(Exception):
    def __init__(self, code: str, message: str, stage: str = "MODEL_CALL"):
        super().__init__(message)
        self.code = code
        self.safe_message = message
        self.stage = stage
