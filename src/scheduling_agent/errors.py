"""Safe, structured application failures."""


class DomainError(Exception):
    def __init__(self, code: str, message: str, status: int = 409):
        self.code = code
        self.message = message
        self.status = status
        super().__init__(message)
