"""Exceptions for the Polestar API client."""


class PolestarError(Exception):
    """Base exception for all Polestar API errors."""


class AuthError(PolestarError):
    """Authentication failed."""


class TokenExpiredError(AuthError):
    """Token has expired and refresh failed."""


class AuthCooldownError(PolestarError):
    """A password login was attempted recently; not retrying yet.

    Deliberately not an ``AuthError``: the failed login may have been a network
    blip, so callers should treat this as transient rather than as rejected
    credentials.
    """


class ApiError(PolestarError):
    """An API call failed."""

    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class NetworkError(PolestarError):
    """A network-level error occurred (connection refused, DNS, timeout)."""


class VehicleUnavailableError(PolestarError):
    """The vehicle is offline or in a mode that prevents communication."""
