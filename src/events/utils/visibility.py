"""Pure id-gathering helpers shared by the ``for_user()`` visibility querysets.

Every ``for_user()`` in ``events.models`` and ``polls.models`` composes the same
primitives: which organizations shut the user out entirely, which ones they are a
valid member of, which rows they own or staff, and which events they hold a
ticket, RSVP or invitation for. Keeping the primitives here means the security
boundary is edited in one place.

Like ``events/utils/blacklist.py`` this module is side-effect free and imports
models lazily, so it is safe to import from model managers (which must not
depend on the service layer). See ``events/CLAUDE.md``:
``controllers → services → models/utils``.
"""

import typing as t

from django.db.models import Q

from events.utils.blacklist import get_hard_blacklisted_org_ids

if t.TYPE_CHECKING:
    from uuid import UUID

    from django.db.models import QuerySet

    from accounts.models import RevelUser
    from events.models import EventInvitation, EventRSVP, OrganizationMember, Ticket


def get_excluded_org_ids(user: "RevelUser") -> set["UUID"]:
    """Organizations the user is BANNED from or hard-blacklisted in.

    Such users see nothing from those organizations, not even PUBLIC content.
    Materialised as a set because callers feed it to ``~Q(...__in=...)`` /
    ``.exclude(id__in=...)``, where a small literal list beats a correlated subquery.
    """
    from events.models import OrganizationMember

    banned_org_ids = OrganizationMember.objects.filter(
        user=user, status=OrganizationMember.MembershipStatus.BANNED
    ).values_list("organization_id", flat=True)
    return set(banned_org_ids) | set(get_hard_blacklisted_org_ids(user))


def get_valid_member_org_ids(user: "RevelUser") -> "QuerySet[OrganizationMember, UUID]":
    """Organizations where the user holds a membership that grants visibility.

    Routes through ``OrganizationMember.objects.for_visibility()`` so CANCELLED and
    BANNED rows never match. Always filter the *parent* through
    ``organization_id__in=<this>`` rather than ``.filter(memberships__user=user)
    .exclude(memberships__status__in=...)``: an exclude on a multi-valued relation
    drops every organization that has *any* cancelled/banned member, not just this one.
    """
    from events.models import OrganizationMember

    return OrganizationMember.objects.for_visibility().filter(user=user).values_list("organization_id", flat=True)


def owner_or_staff_q(user: "RevelUser", org_lookup: str = "organization") -> Q:
    """``Q`` matching rows whose organization the user owns or staffs.

    Args:
        user: The viewer.
        org_lookup: Lookup path from the queried model to its organization
            (``"organization"`` for events/series/resources, ``"event__organization"``
            for ticket tiers).
    """
    return Q(**{f"{org_lookup}__owner": user}) | Q(**{f"{org_lookup}__staff_members": user})


def get_ticketed_event_ids(user: "RevelUser", *, include_cancelled: bool) -> "QuerySet[Ticket, UUID]":
    """Events the user holds a ticket for.

    ``include_cancelled`` is the settled cancelled-ticket rule, made explicit per
    call site: event *listing* visibility (``EventQuerySet.for_user``) keeps
    cancelled tickets so a holder can still open the event their ticket referred
    to, whereas every fine-grained check (address, private resources, polls,
    cancellation reason) requires a live ticket and passes ``False``.
    """
    from events.models import Ticket

    qs = Ticket.objects.filter(user=user)
    if not include_cancelled:
        qs = qs.exclude(status=Ticket.TicketStatus.CANCELLED)
    return qs.values_list("event_id", flat=True)


def get_rsvp_event_ids(user: "RevelUser", *, confirmed_only: bool) -> "QuerySet[EventRSVP, UUID]":
    """Events the user has RSVP'd to.

    Event listing visibility counts any RSVP row (a "no" still means the user was
    asked); fine-grained checks require ``status=YES`` and pass ``confirmed_only=True``.
    """
    from events.models import EventRSVP

    qs = EventRSVP.objects.filter(user=user)
    if confirmed_only:
        qs = qs.filter(status=EventRSVP.RsvpStatus.YES)
    return qs.values_list("event_id", flat=True)


def get_invited_event_ids(user: "RevelUser") -> "QuerySet[EventInvitation, UUID]":
    """Events the user holds a direct invitation to."""
    from events.models import EventInvitation

    return EventInvitation.objects.filter(user=user).values_list("event_id", flat=True)
