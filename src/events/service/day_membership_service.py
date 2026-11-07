"""Day memberships: TEMPORARY register records granted by tickets (#1320).

A ticket whose tier has ``grants_membership_tier`` set grants a TEMPORARY ``MembershipRecord`` covering the
event window. Records never touch ``OrganizationMember`` (no platform privileges); they exist for the
organization's legal register and for the door-scan ``membership`` block.

All writes live here. Hook sites: the Ticket ``post_save`` receiver (``on_ticket_status_change``) and the
bulk writers that skip signals (``on_tickets_issued``).
"""

import typing as t
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from common.utils import get_or_create_with_race_protection
from events.models import (
    Event,
    MembershipRecord,
    MembershipRecordKind,
    MembershipTier,
    OrganizationQuestionnaire,
    Ticket,
    TicketTier,
)
from events.service.member_register_service import _snapshot_full_name
from questionnaires.models import QuestionnaireEvaluation, QuestionnaireSubmission

_ATTENDING = tuple(Ticket.ATTENDING_STATUSES)


@dataclass(frozen=True)
class MembershipSummary:
    """What door staff see about a scanned person's membership."""

    kind: MembershipRecordKind
    tier_name: str
    valid_until: datetime | None


def _event_window(event: Event) -> tuple[datetime, datetime]:
    return event.start, max(event.end, event.start)


def _admission_evidence(ticket: Ticket, event: Event) -> QuestionnaireSubmission | None:
    """Newest READY, non-rejected admission submission by the holder for this event (or its series)."""
    scope = Q(questionnaire__org_questionnaires__events=event)
    if event.event_series_id:
        scope |= Q(questionnaire__org_questionnaires__event_series=event.event_series_id)
    return (
        QuestionnaireSubmission.objects.ready()
        .filter(
            scope,
            user_id=ticket.user_id,
            questionnaire__org_questionnaires__questionnaire_type=OrganizationQuestionnaire.QuestionnaireType.ADMISSION,
        )
        .exclude(evaluation__status=QuestionnaireEvaluation.QuestionnaireEvaluationStatus.REJECTED)
        .select_related("questionnaire")
        .first()
    )


def _grant(ticket: Ticket, granted_tier: MembershipTier, event: Event) -> None:
    """Create (or reopen) the ticket's day-membership record. Idempotent: one row per ticket."""
    valid_from, valid_until = _event_window(event)
    with transaction.atomic():
        existing = MembershipRecord.objects.select_for_update().filter(ticket=ticket).first()
        if existing is not None:
            if existing.ended_reason == MembershipRecord.EndedReason.REVOKED:
                existing.ended_reason = None
                existing.valid_from = valid_from
                existing.valid_until = valid_until
                existing.save(update_fields=["ended_reason", "valid_from", "valid_until", "updated_at"])
            return
        user = ticket.user
        submission = _admission_evidence(ticket, event)
        get_or_create_with_race_protection(
            MembershipRecord,
            Q(ticket=ticket),
            {
                "organization_id": event.organization_id,
                "user": user,
                "full_name": _snapshot_full_name(user),
                "email": user.email,
                "kind": MembershipRecord.Kind.TEMPORARY,
                "tier": granted_tier,
                "tier_name": granted_tier.name,
                "valid_from": valid_from,
                "valid_until": valid_until,
                "source": MembershipRecord.Source.TICKET,
                "ticket": ticket,
                "event": event,
                "questionnaire_submission": submission,
                "accepted_at": (submission.submitted_at or submission.created_at) if submission else None,
                "questionnaire_name": submission.questionnaire.name if submission else "",
            },
        )


def _revoke(ticket: Ticket) -> None:
    """Revoke the ticket's record if the event has not started and the holder never checked in."""
    if ticket.checked_in_at is not None:
        return
    with transaction.atomic():
        record = (
            MembershipRecord.objects.select_for_update()
            .exclude(ended_reason=MembershipRecord.EndedReason.REVOKED)
            .filter(ticket=ticket)
            .first()
        )
        if record is None or ticket.event.start <= timezone.now():
            return
        record.ended_reason = MembershipRecord.EndedReason.REVOKED
        record.save(update_fields=["ended_reason", "updated_at"])


def on_ticket_status_change(ticket: Ticket, old_status: str | None) -> None:
    """Grant or revoke the day membership for a ticket whose committed status moved from ``old_status``."""
    if old_status == ticket.status:
        return
    was_attending = old_status in _ATTENDING
    if ticket.status in _ATTENDING and not was_attending:
        granted = ticket.tier.grants_membership_tier
        if granted is not None:
            _grant(ticket, granted, ticket.event)
    elif was_attending and ticket.status not in _ATTENDING and old_status != Ticket.TicketStatus.CHECKED_IN:
        _revoke(ticket)


def on_tickets_issued(tickets: Sequence[Ticket], old_status: str | None = None) -> None:
    """Grant day memberships for bulk-written tickets that are now attending (bulk writers skip signals)."""
    if old_status in _ATTENDING:
        return
    attending = [ticket for ticket in tickets if ticket.status in _ATTENDING]
    if not attending:
        return
    tiers = {
        tier.pk: tier
        for tier in TicketTier.objects.filter(
            pk__in={ticket.tier_id for ticket in attending}, grants_membership_tier__isnull=False
        ).select_related("grants_membership_tier")
    }
    if not tiers:
        return
    granting = [ticket for ticket in attending if ticket.tier_id in tiers]
    events = Event.objects.in_bulk({ticket.event_id for ticket in granting})
    for ticket in granting:
        granted = t.cast(MembershipTier, tiers[ticket.tier_id].grants_membership_tier)
        _grant(ticket, granted, events[ticket.event_id])


def revoke_upcoming_for_ban(organization_id: UUID, user_id: UUID) -> int:
    """Revoke the user's TEMPORARY records in the org that have not started yet. Ongoing/past ones stay."""
    return (
        MembershipRecord.objects.filter(
            organization_id=organization_id,
            user_id=user_id,
            kind=MembershipRecord.Kind.TEMPORARY,
            valid_from__gt=timezone.now(),
        )
        .exclude(ended_reason=MembershipRecord.EndedReason.REVOKED)
        .update(ended_reason=MembershipRecord.EndedReason.REVOKED, updated_at=timezone.now())
    )


def resync_event_window(event: Event) -> int:
    """Move the event's non-revoked day-membership records to the event's current dates."""
    valid_from, valid_until = _event_window(event)
    return (
        MembershipRecord.objects.filter(event=event, kind=MembershipRecord.Kind.TEMPORARY)
        .exclude(ended_reason=MembershipRecord.EndedReason.REVOKED)
        .update(valid_from=valid_from, valid_until=valid_until, updated_at=timezone.now())
    )


def membership_summary(organization_id: UUID, user_id: UUID, *, ticket: Ticket | None) -> MembershipSummary | None:
    """The person's membership for the door: an open STANDARD record wins, else the ticket's day record.

    One query: both candidates are fetched together and ``kind`` sorts ``standard`` before ``temporary``.
    """
    standard = Q(kind=MembershipRecord.Kind.STANDARD, valid_until__isnull=True)
    candidates = standard if ticket is None else standard | Q(kind=MembershipRecord.Kind.TEMPORARY, ticket=ticket)
    record = (
        MembershipRecord.objects.filter(candidates, organization_id=organization_id, user_id=user_id)
        .exclude(ended_reason=MembershipRecord.EndedReason.REVOKED)
        .order_by("kind")
        .first()
    )
    if record is None:
        return None
    if record.kind == MembershipRecord.Kind.STANDARD:
        return MembershipSummary(MembershipRecord.Kind.STANDARD, record.tier_name, None)
    return MembershipSummary(MembershipRecord.Kind.TEMPORARY, record.tier_name, record.valid_until)
