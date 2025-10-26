from django.core.exceptions import ValidationError as DjangoValidationError


class InvalidResourceStateError(DjangoValidationError):
    """Raised when an EventResource has an invalid combination of fields for its type."""


class AlreadyMemberError(Exception):
    """Raised when a user is already a member of an organization."""


class PendingMembershipRequestExistsError(Exception):
    """Raised when a pending membership request already exists for a user and organization."""


class AlreadyInvitedError(Exception):
    """Raised when a user is already invited to an event."""


class PendingInvitationRequestExistsError(Exception):
    """Raised when a pending invitation request already exists for a user and event."""


class TooManyItemsError(Exception):
    """Raised when too many PotluckItems are created."""
