class PulseError(Exception):
    """Base error for expected application failures."""


class InvalidMessageError(PulseError):
    """Raised when a user message cannot be processed."""


class ProviderError(PulseError):
    """Raised when an external provider fails safely."""

