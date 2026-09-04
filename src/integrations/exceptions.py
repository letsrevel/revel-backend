"""Exceptions raised by providers and by the integrations service layer."""

from integrations.schema import IntegrationErrorCode


class ProviderError(Exception):
    """A provider call failed. Raised by ``ListingProvider`` implementations only.

    The service layer translates it into ``IntegrationError`` (sync endpoints) or into a
    link/connection ``last_error`` (tasks). ``retryable`` marks 429/5xx-class failures.
    """

    def __init__(
        self, code: IntegrationErrorCode, provider_message: str | None = None, *, retryable: bool = False
    ) -> None:
        """Initialize a provider error with a code and optional message."""
        super().__init__(provider_message or code.value)
        self.code = code
        self.provider_message = provider_message
        self.retryable = retryable


class IntegrationError(Exception):
    """A request-level failure with a stable code; rendered by ``integrations.exception_handlers``."""

    def __init__(
        self,
        code: IntegrationErrorCode,
        message: str,
        provider_message: str | None = None,
        *,
        status: int = 400,
    ) -> None:
        """Initialize an integration error with a code, message, and HTTP status."""
        super().__init__(message)
        self.code = code
        self.message = message
        self.provider_message = provider_message
        self.status = status
