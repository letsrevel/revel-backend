"""Whether an account is *known* to an organization through its own acts.

Rule: staff-originated rows (a box-office sale or comp, a staff RSVP, a staff-made event invitation)
show staff only what staff supplied, and confer nothing. Profile data and standing come only from acts
of the account holder. ``known_holder_q`` (in ``events.utils.holder_visibility``, re-exported here) is the
single definition; use ``is_known_holder`` for one account and ``annotate_holder_known`` for a list (one
query, no N+1).
"""

import typing as t
from uuid import UUID

from django.db.models import BooleanField, ExpressionWrapper, OuterRef, QuerySet

from accounts.models import RevelUser
from events.exceptions import InvitationRequiredError
from events.models import Organization
from events.utils.holder_visibility import STAFF_SALE_SOURCES, known_holder_q, self_linked_record_q

__all__ = [
    "STAFF_SALE_SOURCES",
    "annotate_holder_known",
    "is_known_holder",
    "is_known_holder_id",
    "known_holder_q",
    "require_known_holder",
    "self_linked_record_q",
]

_M = t.TypeVar("_M", bound=t.Any)


def is_known_holder_id(organization_id: UUID, user_id: UUID) -> bool:
    """Whether the account ``user_id`` is known to the organization through its own acts (one query)."""
    return RevelUser.objects.filter(pk=user_id).filter(known_holder_q(organization_id, OuterRef("pk"))).exists()


def is_known_holder(organization: Organization, user: RevelUser) -> bool:
    """Whether ``user`` is known to ``organization`` through their own acts (one query)."""
    return is_known_holder_id(organization.pk, user.pk)


def annotate_holder_known(qs: QuerySet[_M], organization_id: UUID, user_field: str = "user_id") -> QuerySet[_M]:
    """Annotate ``holder_known`` (bool) on each row of ``qs``, judged on the account at ``user_field``."""
    return t.cast(
        "QuerySet[_M]",
        qs.annotate(
            holder_known=ExpressionWrapper(
                known_holder_q(organization_id, OuterRef(user_field)), output_field=BooleanField()
            )
        ),
    )


def require_known_holder(organization: Organization, user: RevelUser) -> None:
    """Raise ``InvitationRequiredError`` (400 ``invitation_required``) unless ``user`` is a known holder."""
    if not is_known_holder(organization, user):
        raise InvitationRequiredError
