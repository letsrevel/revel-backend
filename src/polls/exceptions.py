"""Polls app exceptions."""

import typing as t

from django.core.exceptions import ValidationError


class PollAnonymityImmutableError(ValidationError):
    """Raised when staff_anonymous or public_anonymous is changed after creation."""

    def __init__(self, message: str | None = None, **kwargs: t.Any) -> None:
        """Initialise with the default anonymity-lock message when no override is supplied."""
        super().__init__(message or "Anonymity flags are immutable after a poll is created.", **kwargs)


class PollNotOpenError(ValidationError):
    """Raised when a vote action targets a poll that is not OPEN."""

    def __init__(self, message: str | None = None, **kwargs: t.Any) -> None:
        """Initialise with the default not-open message when no override is supplied."""
        super().__init__(message or "This poll is not open for voting.", **kwargs)


class PollNotEligibleError(ValidationError):
    """Raised when a user is not eligible to vote on a poll."""

    def __init__(self, message: str | None = None, **kwargs: t.Any) -> None:
        """Initialise with the default not-eligible message when no override is supplied."""
        super().__init__(message or "You are not eligible to vote on this poll.", **kwargs)


class PollVoteAlreadyCastError(ValidationError):
    """Raised when a user tries to vote twice on a poll that does not allow vote changes."""

    def __init__(self, message: str | None = None, **kwargs: t.Any) -> None:
        """Initialise with the default already-voted message when no override is supplied."""
        super().__init__(message or "You have already voted on this poll.", **kwargs)


class PollVoteChangesNotAllowedError(ValidationError):
    """Raised when a withdraw is attempted on a poll that does not allow vote changes."""

    def __init__(self, message: str | None = None, **kwargs: t.Any) -> None:
        """Initialise with the default changes-not-allowed message when no override is supplied."""
        super().__init__(message or "Vote changes are not allowed for this poll.", **kwargs)


class PollQuestionLockedError(ValidationError):
    """Raised when a question/section/option mutation is attempted on a poll past DRAFT."""

    def __init__(self, message: str | None = None, **kwargs: t.Any) -> None:
        """Initialise with the default question-locked message when no override is supplied."""
        super().__init__(message or "Questions cannot be modified once the poll leaves DRAFT.", **kwargs)


class PollLifecycleError(ValidationError):
    """Raised on invalid status transitions (e.g., reopen with past closes_at)."""

    def __init__(self, message: str | None = None, **kwargs: t.Any) -> None:
        """Initialise with the default lifecycle-error message when no override is supplied."""
        super().__init__(message or "Invalid poll lifecycle action.", **kwargs)


class PollValidationError(ValidationError):
    """Raised when a write payload references unknown / cross-tenant rows.

    Used by ``create_poll`` / ``update_poll`` for membership-tier IDs and by
    ``vote`` for file-upload IDs that don't resolve to actual rows the caller
    owns. Distinct from :class:`PollLifecycleError` (which signals an invalid
    status transition) so controllers can surface the right HTTP code if they
    want to differentiate.
    """

    def __init__(self, message: str | None = None, **kwargs: t.Any) -> None:
        """Initialise with the default validation message when no override is supplied."""
        super().__init__(message or "Invalid references in poll payload.", **kwargs)
