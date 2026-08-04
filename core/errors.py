from __future__ import annotations


class ServiceError(RuntimeError):
    """Stable command-facing error independent of any HTTP transport."""

    def __init__(self, code: str, message: str, data: object = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data


class RuleItemLimitError(RuntimeError):
    pass
