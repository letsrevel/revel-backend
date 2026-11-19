"""Pure query builders for whether an account is *known* to an organization through its own acts.

Rule: staff-originated rows (a box-office sale or comp, a staff RSVP, a staff-made event invitation)
show staff only what staff supplied, and confer nothing. Profile data and standing come only from acts
of the account holder. ``known_holder_q`` is the single definition. This module holds only the ``Q``
builders so models may use them (models import utils, never services); the service module
``events.service.holder_visibility`` re-exports them and adds the query helpers.
"""

from uuid import UUID

from django.db.models import Exists, OuterRef, Q

from events.models import (
    EventInvitationRequest,
    EventRSVP,
    EventWaitList,
    HeldSeriesPass,
    MembershipRecord,
    Organization,
    OrganizationMember,
    OrganizationMembershipRequest,
    OrganizationStaff,
    Ticket,
    TicketSaleSource,
)
from questionnaires.models import QuestionnaireSubmission

STAFF_SALE_SOURCES = (TicketSaleSource.BOX_OFFICE_SALE, TicketSaleSource.BOX_OFFICE_COMP)


def self_linked_record_q() -> Q:
    """Condition on ``MembershipRecord``: the register entry stands for an act of its account holder.

    A day membership inherits its ticket's origin (a box-office ticket's record does not count), and a
    record linked by a staff merge does not count (the merge links it on staff's word alone).
    """
    self_ticket = Ticket.objects.filter(pk=OuterRef("ticket_id")).exclude(sale_source__in=STAFF_SALE_SOURCES)
    return (~Q(source=MembershipRecord.Source.TICKET) | Exists(self_ticket)) & ~Q(
        linked_via=MembershipRecord.LinkMethod.MERGE
    )


def known_holder_q(organization_id: UUID, user_ref: OuterRef) -> Q:
    """Condition: the account at ``user_ref`` acted toward the organization (or is its owner/staff/member).

    Counts: owner, staff, member (any status), membership request (any status), a linked register record
    (see ``self_linked_record_q``: not a box-office day membership, not merge-linked; a day membership
    counts only while its ticket is attending), an attending ticket (active or checked in) not issued by
    the box office (``NULL`` sale source = self, legacy), the holder's own RSVP, a series pass, a
    waitlist entry, an event invitation request, a questionnaire submission. Not counted: box-office
    tickets, pending or cancelled tickets, staff RSVPs, event invitations, waitlist offers.

    Args:
        organization_id: The organization.
        user_ref: Reference to the account id in the outer query (e.g. ``OuterRef("user_id")``).
    """
    org, user = organization_id, user_ref
    record = (
        MembershipRecord.objects.filter(organization_id=org, user_id=user)
        .filter(self_linked_record_q())
        .filter(Q(ticket__isnull=True) | Q(ticket__status__in=Ticket.ATTENDING_STATUSES))
    )
    return (
        Q(Exists(Organization.objects.filter(pk=org, owner_id=user)))
        | Q(Exists(OrganizationStaff.objects.filter(organization_id=org, user_id=user)))
        | Q(Exists(OrganizationMember.objects.filter(organization_id=org, user_id=user)))
        | Q(Exists(OrganizationMembershipRequest.objects.filter(organization_id=org, user_id=user)))
        | Q(Exists(record))
        | Q(
            Exists(
                Ticket.objects.filter(
                    event__organization_id=org, user_id=user, status__in=Ticket.ATTENDING_STATUSES
                ).exclude(sale_source__in=STAFF_SALE_SOURCES)
            )
        )
        | Q(Exists(EventRSVP.objects.filter(event__organization_id=org, user_id=user, source=EventRSVP.Source.SELF)))
        | Q(Exists(HeldSeriesPass.objects.filter(series_pass__event_series__organization_id=org, user_id=user)))
        | Q(Exists(EventWaitList.objects.filter(event__organization_id=org, user_id=user)))
        | Q(Exists(EventInvitationRequest.objects.filter(event__organization_id=org, user_id=user)))
        | Q(
            Exists(
                QuestionnaireSubmission.objects.filter(
                    questionnaire__org_questionnaires__organization_id=org, user_id=user
                )
            )
        )
    )

