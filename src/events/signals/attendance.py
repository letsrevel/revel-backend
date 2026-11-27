"""Receivers for RSVPs, tickets, invitations and the waitlist."""

import typing as t

import structlog
from django.db.models.signals import post_delete, post_save, pre_save
from django.dispatch import receiver

from events.models import (
    EventInvitation,
    EventRSVP,
    EventWaitList,
    Ticket,
)
from events.service import (
    day_membership_service,
    ticket_number_service,
)
from events.service.potluck_service import release_potluck_claims_unless_entitled
from events.tasks.attendees import dispatch_attendee_rebuild

# The logger name of the module before the split, so the log names stay the same.
logger = structlog.get_logger("events.signals")


@receiver(post_save, sender=EventRSVP)
def handle_event_rsvp_save(sender: type[EventRSVP], instance: EventRSVP, created: bool, **kwargs: t.Any) -> None:
    """Trigger visibility task and unclaim potluck items after RSVP is changed or created.

    When a user's RSVP status changes to anything other than YES (i.e., NO or MAYBE),
    their potluck claims are released unless they are otherwise entitled to hold them
    (an attending ticket, or organization owner/staff).
    """
    dispatch_attendee_rebuild(instance.event_id)

    if instance.status in [EventRSVP.RsvpStatus.NO, EventRSVP.RsvpStatus.MAYBE]:
        release_potluck_claims_unless_entitled(instance.event_id, instance.user_id)


@receiver(post_delete, sender=EventRSVP)
def handle_event_rsvp_delete(sender: type[EventRSVP], instance: EventRSVP, **kwargs: t.Any) -> None:
    """Trigger visibility task and unclaim potluck items after RSVP is deleted.

    Claims are released unless the user is otherwise entitled to hold them.
    """
    dispatch_attendee_rebuild(instance.event_id)
    release_potluck_claims_unless_entitled(instance.event_id, instance.user_id)


@receiver(pre_save, sender=Ticket)
def capture_ticket_old_status_for_potluck(sender: type[Ticket], instance: Ticket, **kwargs: t.Any) -> None:
    """Stamp the committed status on the instance (``None`` for new rows) for the post_save handlers.

    Feeds both the potluck release and the day-membership hook (``on_ticket_status_change``).

    Stamped under a dedicated attribute so this module does not depend on the ordering of the
    notifications app's own Ticket pre_save receivers. A save with ``update_fields`` that
    excludes ``"status"`` cannot change it, so the SELECT is skipped and the in-memory status is
    stamped (read as "no transition").
    """
    update_fields = kwargs.get("update_fields")
    if update_fields is not None and "status" not in update_fields:
        instance._potluck_old_status = instance.status  # type: ignore[attr-defined]
        return
    instance._potluck_old_status = (  # type: ignore[attr-defined]
        Ticket.objects.filter(pk=instance.pk).values_list("status", flat=True).first()
    )


@receiver(post_save, sender=Ticket)
def reset_event_address_cache(sender: type[Ticket], instance: Ticket, **kwargs: t.Any) -> None:
    """Drop ``Event.can_user_see_address``'s per-instance cache on an already-loaded event.

    A status change (e.g. PENDING -> ACTIVE) must not leave a stale answer behind in the same
    process. Uses the cached related object only, so it costs no query.
    """
    cached_event = instance._state.fields_cache.get("event")
    if cached_event is not None:
        cached_event.__dict__.pop("_address_visibility_cache", None)


def _dispatch_ticket_visibility_flags(ticket: Ticket) -> None:
    """Rebuild the event's visibility flags at commit; a failure does not skip the later callbacks (#1488)."""
    dispatch_attendee_rebuild(ticket.event_id, ticket_id=str(ticket.pk))


@receiver(post_save, sender=Ticket)
def handle_ticket_visibility_and_potluck(
    sender: type[Ticket], instance: Ticket, created: bool, **kwargs: t.Any
) -> None:
    """Trigger the visibility task and release potluck claims when a ticket stops counting.

    Claims are released on a *transition* out of attendance: an attending ticket (ACTIVE or
    CHECKED_IN) becoming PENDING or CANCELLED, or an existing ticket of any status becoming
    CANCELLED (covers a pre-existing claim by a PENDING holder). Only when the user is no
    longer entitled to hold claims (another attending ticket, a YES RSVP, or org owner/staff).
    Creation never releases: owners/staff and YES-RSVPs may claim without a ticket.

    Note: This is one of multiple post_save handlers for Ticket model:
    - events.signals.attendance.handle_ticket_visibility_and_potluck: Handles visibility flags + potluck (this handler)
    - notifications.signals.ticket.handle_ticket_notifications: Sends notifications
    - notifications.signals.waitlist.handle_ticket_waitlist_logic: Manages waitlist removal
    """
    _dispatch_ticket_visibility_flags(instance)

    old_status = getattr(instance, "_potluck_old_status", None)
    new_status = instance.status
    if (
        old_status is not None
        and old_status != new_status
        and new_status not in Ticket.ATTENDING_STATUSES
        and (old_status in Ticket.ATTENDING_STATUSES or new_status == Ticket.TicketStatus.CANCELLED)
    ):
        release_potluck_claims_unless_entitled(instance.event_id, instance.user_id)


@receiver(post_save, sender=Ticket)
def sync_day_membership_on_ticket_save(sender: type[Ticket], instance: Ticket, **kwargs: t.Any) -> None:
    """Grant / revoke the ticket's day membership (#1320); bulk writers call the service themselves."""
    if not kwargs.get("raw"):
        day_membership_service.on_ticket_status_change(instance, getattr(instance, "_potluck_old_status", None))


@receiver(post_save, sender=Ticket)
def assign_ticket_number_on_issue(sender: type[Ticket], instance: Ticket, **kwargs: t.Any) -> None:
    """Give a ticket its fiscal number (assigned without gaps) once it is issued (ACTIVE/CHECKED_IN).

    Covers every ``save()`` path; the bulk writers that skip signals call
    ``ticket_number_service.assign_ticket_numbers`` themselves.
    """
    if not kwargs.get("raw"):
        ticket_number_service.assign_ticket_numbers([instance])


@receiver(post_delete, sender=Ticket)
def handle_ticket_delete(sender: type[Ticket], instance: Ticket, **kwargs: t.Any) -> None:
    """Trigger visibility task and unclaim potluck items after Ticket is deleted.

    Claims are released unless the user is still entitled to hold them (e.g. another attending
    ticket, a YES RSVP, or org owner/staff).
    """
    _dispatch_ticket_visibility_flags(instance)
    release_potluck_claims_unless_entitled(instance.event_id, instance.user_id)


@receiver(post_delete, sender=EventInvitation)
def handle_invitation_delete(sender: type[EventInvitation], instance: EventInvitation, **kwargs: t.Any) -> None:
    """Trigger visibility task after invitation is deleted."""
    dispatch_attendee_rebuild(instance.event_id)


@receiver(post_delete, sender=EventWaitList)
def handle_waitlist_entry_deleted(sender: type[EventWaitList], instance: EventWaitList, **kwargs: t.Any) -> None:
    """Revoke any PENDING WaitlistOffer for (event, user) when the entry is removed.

    Policy note: the rest of the advanced-waitlist feature uses **explicit**
    ``enqueue_waitlist_processing(event_id)`` calls at every capacity-freeing
    site rather than signals (see docs/superpowers/specs/2026-05-19-advanced-waitlist-design.md
    decisions log). This handler is the deliberate exception: ``EventWaitList``
    rows are deleted from many paths — admin ``delete_waitlist_entry``,
    user-side ``leave_waitlist``, ``EventManager._claim_active_offer``,
    ``BatchTicketService.claim_waitlist_offer_if_any``, ad-hoc ORM
    ``.filter().delete()`` calls — and wiring each one inline is repetitive
    and easy to miss. Callers should still document inline that the offer
    revoke is handled here so future readers don't have to chase it down.

    Idempotent: only PENDING offers are affected. CLAIMED offers (deletion happens
    AFTER claim flips status) and EXPIRED offers (leave_waitlist flips status BEFORE
    delete) are not touched.
    """
    from events.models import WaitlistOffer
    from events.service.waitlist_service import enqueue_waitlist_processing

    # Use a conditional UPDATE so a concurrent claim/expire flip cannot be
    # silently clobbered: we only enqueue if THIS query actually transitioned
    # a row, and the database — not Python — decides which writer wins.
    affected = WaitlistOffer.objects.filter(
        event_id=instance.event_id,
        user_id=instance.user_id,
        status=WaitlistOffer.WaitlistOfferStatus.PENDING,
    ).update(status=WaitlistOffer.WaitlistOfferStatus.REVOKED)
    if affected:
        enqueue_waitlist_processing(instance.event_id)
