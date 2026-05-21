from django.core.exceptions import ValidationError as DjangoValidationError


class InvalidResourceStateError(DjangoValidationError):
    """Raised when an EventResource has an invalid combination of fields for its type."""


class AlreadyMemberError(Exception):
    """Raised when a user is already a member of an organization."""


class PendingMembershipRequestExistsError(Exception):
    """Raised when a pending membership request already exists for a user and organization."""


class TooManyItemsError(Exception):
    """Raised when too many PotluckItems are created."""


class OrganizationTokenStaffGrantForbidden(Exception):
    """Raised when a non-owner attempts to manage a staff-granting organization token."""


class OrganizationTokenGrantInvariantError(Exception):
    """Raised when an organization-token update would leave both grants disabled."""
