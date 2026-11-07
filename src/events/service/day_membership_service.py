"""Day memberships: TEMPORARY register records granted by tickets (#1320).

A ticket whose tier has ``grants_membership_tier`` set grants a TEMPORARY ``MembershipRecord`` covering the
event window. Records never touch ``OrganizationMember`` (no platform privileges); they exist for the
organization's legal register and for the door-scan ``membership`` block.

All writes live here. Hook sites: the Ticket ``post_save`` receiver (``on_ticket_status_change``) and the
bulk writers that skip signals (``on_tickets_issued``).
"""

import typing as t
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID

from django.db import transaction
from django.db.models import Case, DateTimeField, Exists, ExpressionWrapper, F, IntegerField, OuterRef, Q, Value, When
from django.utils import timezone

from common.utils import get_or_create_with_race_protection
from events.models import (
    Event,
    EventQuestionnaireSubmission,
    MembershipRecord,
    MembershipRecordKind,
    MembershipTier,
    OrganizationQuestionnaire,
    Ticket,
    TicketTier,
)
from events.service.member_register_service import _snapshot_full_name
from events.utils import membership_access
from questionnaires.models import Questionnaire, QuestionnaireEvaluation, QuestionnaireSubmission

_ATTENDING = tuple(Ticket.ATTENDING_STATUSES)


@dataclass(frozen=True)
class MembershipSummary:
    """What door staff see about a scanned person's membership."""

    kind: MembershipRecordKind
    tier_name: str
    valid_until: datetime | None


def _event_window(event: Event) -> tuple[datetime, datetime]:
    return event.start, max(event.end, event.start)


def _admission_evidence(user_id: UUID, event: Event, *, via_pass: bool = False) -> QuestionnaireSubmission | None:
    """Newest READY admission submission the admission gate would accept for this event (or its series).

    Mirrors ``event_manager``: PUBLISHED questionnaire, a ``per_event`` one needs a submission made for THIS
    event, a rejected evaluation never counts, and when the org questionnaire requires evaluation a
    pending-review one does not count either, nor does an approval older than
    ``max_submission_age``. For a ticket issued by a series pass (``via_pass``) a
    series-linked ``per_event`` questionnaire is also satisfied by a submission for ANY event of the series,
    which is what the pass purchase gate accepted.
    """
    scope = Q(questionnaire__org_questionnaires__events=event)
    if event.event_series_id:
        scope |= Q(questionnaire__org_questionnaires__event_series=event.event_series_id)
    for_this_event = Exists(EventQuestionnaireSubmission.objects.filter(event=event, submission=OuterRef("pk")))
    evaluation_status = QuestionnaireEvaluation.QuestionnaireEvaluationStatus
    per_event_ok = Q(questionnaire__org_questionnaires__per_event=False) | Q(for_this_event)
    if via_pass and event.event_series_id:
        for_series = Exists(
            EventQuestionnaireSubmission.objects.filter(
                event__event_series_id=event.event_series_id, submission=OuterRef("pk")
            )
        )
        per_event_ok |= Q(questionnaire__org_questionnaires__event_series=event.event_series_id) & Q(for_series)
    return (
        QuestionnaireSubmission.objects.ready()
        .filter(
            scope,
            per_event_ok,
            user_id=user_id,
            questionnaire__status=Questionnaire.QuestionnaireStatus.PUBLISHED,
            questionnaire__org_questionnaires__questionnaire_type=OrganizationQuestionnaire.QuestionnaireType.ADMISSION,
        )
        .exclude(evaluation__status=evaluation_status.REJECTED)
        .exclude(
            Q(questionnaire__org_questionnaires__requires_evaluation=True),
            evaluation__status=evaluation_status.PENDING_REVIEW,
        )
        .exclude(
            Q(questionnaire__org_questionnaires__requires_evaluation=True),
            Q(questionnaire__org_questionnaires__max_submission_age__gt=timedelta(0)),
            evaluation__status=evaluation_status.APPROVED,
            evaluation__updated_at__lt=ExpressionWrapper(
                Value(timezone.now(), output_field=DateTimeField())
                - F("questionnaire__org_questionnaires__max_submission_age"),
                output_field=DateTimeField(),
            ),
        )
        .select_related("questionnaire")
        .order_by(F("submitted_at").desc(nulls_last=True), "-created_at")
        .first()
    )


def _is_banned(organization_id: UUID, user_id: UUID) -> bool:
    return membership_access.banned_memberships().filter(organization_id=organization_id, user_id=user_id).exists()


def _record_kwargs(
    ticket: Ticket, granted_tier: MembershipTier, event: Event, submission: QuestionnaireSubmission | None
) -> dict[str, t.Any]:
    valid_from, valid_until = _event_window(event)
    user = ticket.user
    return {
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
    }


def _sync_locked_windows(events: Iterable[Event]) -> None:
    """Re-read each event's dates under a row lock, so a concurrent date edit can't be missed.

    A grant that read the event before the edit committed would otherwise write a record with the old window
    after ``resync_event_window`` already ran. Locking the event row first matches the edit path's lock order.
    """
    by_id = {event.pk: event for event in events}
    rows = Event.objects.select_for_update().filter(pk__in=by_id).order_by("pk").values_list("pk", "start", "end")
    for pk, start, end in rows:
        by_id[pk].start, by_id[pk].end = start, end


def _grant(ticket: Ticket, granted_tier: MembershipTier, event: Event) -> None:
    """Create (or reopen) the ticket's day-membership record. Idempotent: one row per ticket.

    A banned person is granted nothing and a ban-revoked record is not reopened. The ban is checked inside
    the transaction, right before the write, so a concurrent ban cannot slip between check and write.
    """
    with transaction.atomic():
        _sync_locked_windows([event])
        valid_from, valid_until = _event_window(event)
        if _is_banned(event.organization_id, ticket.user_id):
            return
        existing = MembershipRecord.objects.select_for_update().filter(ticket=ticket).first()
        if existing is not None:
            if existing.ended_reason == MembershipRecord.EndedReason.REVOKED:
                existing.ended_reason = None
                existing.valid_from = valid_from
                existing.valid_until = valid_until
                existing.save(update_fields=["ended_reason", "valid_from", "valid_until", "updated_at"])
            return
        kwargs = _record_kwargs(
            ticket,
            granted_tier,
            event,
            _admission_evidence(ticket.user_id, event, via_pass=ticket.held_pass_id is not None),
        )
        get_or_create_with_race_protection(MembershipRecord, Q(ticket=ticket), kwargs)


def _revoke(ticket: Ticket, *, ignore_start: bool = False) -> None:
    """Revoke the ticket's record if the event has not started (unless ``ignore_start``) and never checked in."""
    if ticket.checked_in_at is not None:
        return
    with transaction.atomic():
        record = (
            MembershipRecord.objects.select_for_update()
            .exclude(ended_reason=MembershipRecord.EndedReason.REVOKED)
            .filter(ticket=ticket)
            .first()
        )
        if record is None or (not ignore_start and ticket.event.start <= timezone.now()):
            return
        record.ended_reason = MembershipRecord.EndedReason.REVOKED
        record.save(update_fields=["ended_reason", "updated_at"])


def on_ticket_status_change(ticket: Ticket, old_status: str | None, *, revoke_ignoring_start: bool = False) -> None:
    """Grant or revoke the day membership for a ticket whose committed status moved from ``old_status``.

    ``revoke_ignoring_start`` skips the "event has not started" check on revoke, for callers that decided it
    earlier (bulk event cancellation decides at fan-out time). Never revokes after a check-in.
    """
    if old_status == ticket.status:
        return
    was_attending = old_status in _ATTENDING
    if ticket.status in _ATTENDING and not was_attending:
        # ponytail: enabling ``grants_membership_tier`` on a tier with tickets already sold grants nothing
        # retroactively (no status transition happens). Upgrade path: an explicit backfill action.
        granted = ticket.tier.grants_membership_tier
        if granted is not None:
            _grant(ticket, granted, ticket.event)
    elif was_attending and ticket.status not in _ATTENDING and old_status != Ticket.TicketStatus.CHECKED_IN:
        _revoke(ticket, ignore_start=revoke_ignoring_start)


def on_tickets_issued(tickets: Sequence[Ticket], old_status: str | None = None) -> None:
    """Grant day memberships for bulk-written tickets that are now attending (bulk writers skip signals).

    Call BEFORE ``assign_ticket_numbers`` (lock order: ... Ticket -> MembershipRecord -> TicketNumberSequence).
    The cost is constant in the number of tickets (per buyer for the evidence lookup): one lock over the
    tickets' existing records (only when ``old_status`` is set, i.e. a PENDING -> ACTIVE confirm, since freshly
    created tickets have none), reopening revoked ones in bulk, and one ``bulk_create`` for the rest. Bans are
    re-checked inside the transaction, right before the writes.
    """
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
    granting = [ticket for ticket in attending if ticket.tier_id in tiers]
    if not granting:
        return
    events = Event.objects.in_bulk({ticket.event_id for ticket in granting})
    evidence: dict[tuple[UUID, UUID, bool], QuestionnaireSubmission | None] = {}
    for ticket in granting:
        key = (ticket.user_id, ticket.event_id, ticket.held_pass_id is not None)
        if key not in evidence:
            evidence[key] = _admission_evidence(ticket.user_id, events[ticket.event_id], via_pass=key[2])
    _write_records(granting, tiers, events, evidence, lock_existing=old_status is not None)


def _write_records(
    granting: list[Ticket],
    tiers: dict[UUID, TicketTier],
    events: dict[UUID, Event],
    evidence: dict[tuple[UUID, UUID, bool], QuestionnaireSubmission | None],
    *,
    lock_existing: bool,
) -> None:
    """Create / reopen the records for ``granting`` in one transaction, re-checking bans right before the writes."""
    with transaction.atomic():
        _sync_locked_windows(events.values())
        existing: dict[UUID, MembershipRecord] = {}
        if lock_existing:
            existing = {
                record.ticket_id: record  # type: ignore[misc]
                for record in MembershipRecord.objects.select_for_update().filter(
                    ticket_id__in=[ticket.pk for ticket in granting]
                )
            }
        banned = set(
            membership_access.banned_memberships()
            .filter(
                organization_id__in={e.organization_id for e in events.values()},
                user_id__in={ticket.user_id for ticket in granting},
            )
            .values_list("organization_id", "user_id")
        )
        reopened: list[MembershipRecord] = []
        created: list[MembershipRecord] = []
        for ticket in granting:
            event = events[ticket.event_id]
            if (event.organization_id, ticket.user_id) in banned:
                continue
            record = existing.get(ticket.pk)
            if record is None:
                granted = t.cast(MembershipTier, tiers[ticket.tier_id].grants_membership_tier)
                key = (ticket.user_id, ticket.event_id, ticket.held_pass_id is not None)
                created.append(MembershipRecord(**_record_kwargs(ticket, granted, event, evidence[key])))
            elif _reopen(record, event):
                reopened.append(record)
        if reopened:
            MembershipRecord.objects.bulk_update(reopened, ["ended_reason", "valid_from", "valid_until", "updated_at"])
        MembershipRecord.objects.bulk_create(created)


def _reopen(record: MembershipRecord, event: Event) -> bool:
    """Reset a ban/cancel-revoked record to the event's window in memory; False if it was not revoked."""
    if record.ended_reason != MembershipRecord.EndedReason.REVOKED:
        return False
    record.ended_reason = None
    record.valid_from, record.valid_until = _event_window(event)
    record.updated_at = timezone.now()
    return True


def revoke_for_deleted_event(event: Event) -> int:
    """Revoke the ticket-granted day records (not checked-in ones) of an event deleted before it starts."""
    return (
        MembershipRecord.objects.filter(
            event=event,
            kind=MembershipRecord.Kind.TEMPORARY,
            source=MembershipRecord.Source.TICKET,
            valid_from__gt=timezone.now(),
        )
        .exclude(ended_reason=MembershipRecord.EndedReason.REVOKED)
        .exclude(ticket__checked_in_at__isnull=False)
        .update(ended_reason=MembershipRecord.EndedReason.REVOKED, updated_at=timezone.now())
    )


def revoke_upcoming_for_ban(organization_id: UUID, user_id: UUID) -> int:
    """Revoke the user's TEMPORARY records in the org that have not started yet. Ongoing/past/checked-in ones stay."""
    return (
        MembershipRecord.objects.filter(
            organization_id=organization_id,
            user_id=user_id,
            kind=MembershipRecord.Kind.TEMPORARY,
            valid_from__gt=timezone.now(),
        )
        .exclude(ended_reason=MembershipRecord.EndedReason.REVOKED)
        .exclude(ticket__checked_in_at__isnull=False)
        .update(ended_reason=MembershipRecord.EndedReason.REVOKED, updated_at=timezone.now())
    )


def resync_event_window(event: Event) -> int:
    """Move the event's non-revoked day-membership records to the event's current dates."""
    valid_from, valid_until = _event_window(event)
    return (
        MembershipRecord.objects.filter(
            event=event, kind=MembershipRecord.Kind.TEMPORARY, source=MembershipRecord.Source.TICKET
        )
        .exclude(ended_reason=MembershipRecord.EndedReason.REVOKED)
        .update(valid_from=valid_from, valid_until=valid_until, updated_at=timezone.now())
    )


def membership_summary(organization_id: UUID, user_id: UUID, *, ticket: Ticket | None) -> MembershipSummary | None:
    """The person's membership for the door: an open STANDARD record wins, else the ticket's day record.

    One query: both candidates are fetched together, ordered standard first.
    """
    standard = Q(kind=MembershipRecord.Kind.STANDARD, valid_until__isnull=True)
    candidates = standard if ticket is None else standard | Q(kind=MembershipRecord.Kind.TEMPORARY, ticket=ticket)
    record = (
        MembershipRecord.objects.filter(candidates, organization_id=organization_id, user_id=user_id)
        .exclude(ended_reason=MembershipRecord.EndedReason.REVOKED)
        .order_by(Case(When(kind=MembershipRecord.Kind.STANDARD, then=0), default=1, output_field=IntegerField()))
        .first()
    )
    if record is None:
        return None
    if record.kind == MembershipRecord.Kind.STANDARD:
        return MembershipSummary(MembershipRecord.Kind.STANDARD, record.tier_name, None)
    return MembershipSummary(MembershipRecord.Kind.TEMPORARY, record.tier_name, record.valid_until)
