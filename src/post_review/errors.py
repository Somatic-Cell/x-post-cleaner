"""Errors carry safe codes only: never response bodies, credentials or post text."""

class AppError(Exception):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class ProviderError(AppError):
    def __init__(self, code: str, *, retry_at: float | None = None):
        self.retry_at = retry_at
        super().__init__(code)


class DeleteUnknown(ProviderError):
    """A DELETE may have reached the server. Never automatically resend it."""
