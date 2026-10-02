"""The single error type raised across consortium."""

from __future__ import annotations


class ConsortiumError(Exception):
    """A user-facing failure with a snake_case reason ``code``.

    The CLI prints ``code: message`` to stderr and exits with code 1.
    ``path`` optionally names the file or folder the error is about.
    """

    def __init__(self, code: str, message: str, path: str | None = None) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.path = path

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"
