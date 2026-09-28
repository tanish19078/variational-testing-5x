"""Typed exceptions for the Variational client.

The client maps transport- and HTTP-level failures onto this hierarchy so
callers (the risk engine, the bot loop) can branch on *category* of failure
rather than sniffing status codes.
"""

from __future__ import annotations

from typing import Optional


class VariationalError(Exception):
    """Base class for every error raised by this package."""


class VariationalAPIError(VariationalError):
    """Non-success HTTP response from the API."""

    def __init__(self, status_code: int, message: str, body: str = "") -> None:
        self.status_code = status_code
        self.message = message
        self.body = body
        super().__init__(f"HTTP {status_code}: {message}")


class VariationalAuthError(VariationalAPIError):
    """401 / 403 - session cookie missing, expired, or challenged."""


class VariationalRateLimitError(VariationalAPIError):
    """429 - rate limited. Carries the server's retry hint when present."""

    def __init__(
        self,
        status_code: int,
        message: str,
        body: str = "",
        retry_after_s: Optional[float] = None,
    ) -> None:
        self.retry_after_s = retry_after_s
        super().__init__(status_code, message, body)


class VariationalServerError(VariationalAPIError):
    """5xx - server-side failure."""


class VariationalTimeoutError(VariationalError):
    """The request exceeded the configured timeout."""


class VariationalNetworkError(VariationalError):
    """Connection-level failure (DNS, refused, reset)."""


class KillSwitchError(VariationalError):
    """Raised when the risk engine has halted trading."""
