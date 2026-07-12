"""Object-level permission classes for the polls API.

Mirrors :mod:`events.controllers.permissions`: each class inherits from
:class:`events.controllers.permissions.RootPermission` and implements
``has_object_permission`` so that Django Ninja Extra evaluates the check
against the resolved poll instance returned by
``self.get_object_or_exception(qs, pk=...)``.
"""

import typing as t
from uuid import UUID

from django.http import HttpRequest
from ninja_extra import ControllerBase
from ninja_extra.exceptions import PermissionDenied

from events.controllers.permissions import PermissionMapPermission, RootPermission
from polls.models import Poll


class PollPermission(PermissionMapPermission):
    """Generic poll-action permission.

    Delegates to ``Poll.organization.has_org_permission(user_id, action)``
    using the action passed at construction time (e.g. ``"manage_polls"``).
    The ``action`` is constrained to a real ``PermissionMap`` key (see #683).
    """

    def has_object_permission(
        self,
        request: HttpRequest,
        controller: ControllerBase,
        obj: Poll,
    ) -> bool:
        """Return True iff ``request.user`` has ``self.action`` on the poll's organization."""
        return obj.organization.has_org_permission(t.cast(UUID, request.user.id), self.action)


class IsPollOrganizationOwner(RootPermission):
    """Restricts an action on a poll to the owner of its organization.

    Raises :class:`PermissionDenied` (rather than returning False) so the
    framework surfaces a 403 with a meaningful message instead of falling
    through to a generic ``permissions failed`` error.
    """

    def __init__(self) -> None:
        """Bind the action to ``"is_owner"`` for diagnostic logging."""
        super().__init__(action="is_owner")

    def has_object_permission(
        self,
        request: HttpRequest,
        controller: ControllerBase,
        obj: Poll,
    ) -> bool:
        """Return True iff ``request.user`` owns the poll's organization."""
        if obj.organization.owner_id == request.user.id:
            return True
        raise PermissionDenied("Only the organization owner can perform this action.")


__all__ = ["IsPollOrganizationOwner", "PollPermission"]
