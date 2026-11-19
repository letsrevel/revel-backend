"""Masked account projection for staff-facing responses.

When the only rows tying an account to an organization were created by staff (box-office ticket,
staff RSVP, staff-made invitation), staff see only what they supplied: the email address and the
name they typed. Everything else on the profile is withheld. See ``events.service.holder_visibility``.
"""

import typing as t
from uuid import UUID

from ninja import Schema

from accounts.models import RevelUser
from common.signing import get_file_url


class StaffViewUserSchema(Schema):
    """An account as staff may see it: ``known`` is False for an account staff alone linked in."""

    id: UUID
    known: bool
    display_name: str
    email: str | None = None
    phone_number: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    preferred_name: str | None = None
    pronouns: str | None = None
    bio: str | None = None
    profile_picture_url: str | None = None
    profile_picture_thumbnail_url: str | None = None
    profile_picture_preview_url: str | None = None


def staff_view_user(
    user: RevelUser, *, known: bool, supplied_name: str = "", include_phone: bool = False
) -> StaffViewUserSchema:
    """Project ``user`` for staff.

    Args:
        user: The account behind the row.
        known: Whether the account acted toward the organization (``holder_known``).
        supplied_name: The name staff supplied on the row (e.g. the ticket's guest name).
        include_phone: Whether this surface exposes the phone number for a known account.
    """
    if not known:
        return StaffViewUserSchema(id=user.id, known=False, display_name=supplied_name or user.email, email=user.email)
    return StaffViewUserSchema(
        id=user.id,
        known=True,
        display_name=user.display_name,
        email=user.email,
        phone_number=user.phone_number if include_phone else None,
        first_name=user.first_name,
        last_name=user.last_name,
        preferred_name=user.preferred_name,
        pronouns=user.pronouns,
        bio=user.bio,
        profile_picture_url=get_file_url(user.profile_picture),
        profile_picture_thumbnail_url=get_file_url(user.profile_picture_thumbnail),
        profile_picture_preview_url=get_file_url(user.profile_picture_preview),
    )


def resolve_staff_view_user(
    obj: t.Any,
    organization_id: t.Callable[[t.Any], UUID],
    *,
    supplied_name: str = "",
    include_phone: bool = False,
) -> StaffViewUserSchema:
    """Resolver helper for a row with a ``user``: reads the row's ``holder_known`` annotation.

    List endpoints annotate with ``annotate_holder_known`` so this costs no query per row. A row
    without the annotation (a single object built elsewhere) is judged on the spot.
    """
    user: RevelUser = obj.user
    known = getattr(obj, "holder_known", None)
    if known is None:
        from events.models import Organization
        from events.service.holder_visibility import is_known_holder

        known = is_known_holder(Organization(pk=organization_id(obj)), user)
    return staff_view_user(user, known=bool(known), supplied_name=supplied_name, include_phone=include_phone)
