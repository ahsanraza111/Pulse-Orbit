class PulseError(Exception):
    """Base error for expected application failures."""


class InvalidMessageError(PulseError):
    """Raised when a user message cannot be processed."""


class ProviderError(PulseError):
    """Raised when an external provider fails safely."""


class OrbitError(PulseError):
    """Base error for expected Orbit workflow failures."""


class OrbitNotConfiguredError(OrbitError):
    """Raised when the optional Orbit integration is not configured."""


class OrbitAuthenticationError(OrbitError):
    """Raised when Orbit rejects or can no longer refresh a session."""


class OrbitAuthRequiredError(OrbitError):
    """Raised when a Teams user has not linked an Orbit session."""


class OrbitValidationError(OrbitError):
    """Raised when an entry request is missing or contains invalid data."""


class OrbitNotFoundError(OrbitError):
    """Raised when an Orbit employee, project, task, or draft cannot be found."""


class OrbitAmbiguousMatchError(OrbitError):
    def __init__(self, entity: str, options: list[str]) -> None:
        self.entity = entity
        self.options = options
        super().__init__(f"Multiple {entity} matches: {', '.join(options)}")


class OrbitProviderError(OrbitError):
    """Raised for a definite Orbit provider failure."""


class OrbitMutationUncertainError(OrbitError):
    """Raised when a create request may have reached Orbit before failing."""
