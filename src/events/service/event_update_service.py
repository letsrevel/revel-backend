"""Event update service.

Owns the side-effecting workflows for editing an event:

- ``update_event``: applies an ``EventEditSchema`` payload, marks series
  occurrences as ``is_modified`` when a real field change occurs, and
  triggers waitlist side effects on capacity / waitlist-open transitions.
- ``update_status``: applies a status transition and dispatches the
  matching waitlist side effects (revoke pending offers on CANCELLED,
  re-process waitlist on un-cancel).
- ``update_slug``: enforces per-organization slug uniqueness and persists
  the new slug.

The controller layer (``events/controllers/event_admin/core.py``) must not
read/diff/dispatch around these flows directly; it should call into this
module so all the side effects stay in one place.
"""

import typing as t

from django.db import transaction
from django.utils.translation import gettext_lazy as _

from accounts.models import RevelUser
from common.utils import update_db_instance
from events import models
from events.schema import EventEditSchema
from events.service.waitlist_service import enqueue_waitlist_processing, revoke_all_pending_offers
from events.utils.schedule import EventScheduleSession


class SlugAlreadyExistsError(Exception):
    """Raised when an event slug update collides with an existing event in the same org."""

    def __init__(self, slug: str) -> None:
        """Initialise the error with the slug that collided."""
        super().__init__(f"Event slug '{slug}' already exists in this organization.")
        self.slug = slug


@transaction.atomic
def update_event(
    event: models.Event,
    payload: EventEditSchema,
    *,
    requested_by: RevelUser | None = None,
) -> models.Event:
    """Apply ``payload`` to ``event`` and fire waitlist side effects.

    Behavior:
      * Snapshots the persisted values for the fields the client explicitly
        sent (``model_dump(exclude_unset=True)``) so we can diff against
        the post-update state. This must happen BEFORE
        ``update_db_instance`` mutates the instance.
      * If the event is a series occurrence (``occurrence_index is not None``
        and not already modified) and any of the sent fields actually
        changed, flips ``is_modified=True`` so the occurrence is protected
        from future template propagation. A no-op PUT does NOT mark it
        modified.
      * If ``effective_capacity`` grew, enqueues a waitlist processing
        pass — newly available seats may unblock waitlisted users.
      * If ``waitlist_open`` flipped True → False, revokes all pending
        offers — those users would otherwise see ghost offers for a
        closed waitlist.

    Args:
        event: The event to update. Must not be a template (the controller
            uses ``Event.objects.for_user()`` which already filters those out).
        payload: The validated edit payload.
        requested_by: The user performing the edit. Accepted for audit /
            future use; not currently consumed.

    Returns:
        The refreshed ``Event`` instance after the update.
    """
    del requested_by  # accepted for future audit hooks; unused today

    # Snapshot for occurrence diff. Comparing against ``model_dump`` gives
    # us exactly the fields the client tried to set, which is what
    # ``update_db_instance`` will write.
    track_is_modified = event.occurrence_index is not None and not event.is_modified
    pre_values: dict[str, t.Any] = {}
    if track_is_modified:
        payload_fields = set(payload.model_dump(exclude_unset=True).keys())
        pre_values = {f: getattr(event, f, None) for f in payload_fields if hasattr(event, f)}

    # Snapshot waitlist-relevant state before the update so we can detect
    # capacity increases and waitlist_open True -> False transitions.
    old_effective_capacity = event.effective_capacity
    was_waitlist_open = event.waitlist_open

    updated_event = update_db_instance(event, payload)

    # Mark occurrences as modified only when a persisted field actually
    # changed. Comparing against the pre-update snapshot avoids marking
    # idempotent no-op PUTs as manual edits.
    #
    # The ``!=`` comparison is reliable for the simple types
    # ``EventEditSchema`` actually exposes (str, int, bool, datetime).
    # GIS Point objects or cross-tz datetimes could compare unequal for
    # structurally-identical values, but neither is exposed here.
    if track_is_modified and pre_values:
        changed = any(getattr(updated_event, field) != pre_values[field] for field in pre_values)
        if changed:
            updated_event.is_modified = True
            updated_event.save(update_fields=["is_modified"])

    # If the effective capacity grew, freshly-available seats may unblock
    # waitlisted users — enqueue a processing pass.
    if updated_event.effective_capacity > old_effective_capacity:
        enqueue_waitlist_processing(updated_event.id)

    # If the waitlist was just closed, revoke any pending offers — those
    # users would otherwise see a "ghost" offer for a now-closed waitlist.
    if was_waitlist_open and not updated_event.waitlist_open:
        revoke_all_pending_offers(updated_event.id)

    return updated_event


@transaction.atomic
def update_status(
    event: models.Event,
    new_status: models.Event.EventStatus,
    *,
    cancellation_reason: str | None = None,
) -> models.Event:
    """Transition an event's status and fire matching waitlist side effects.

    Behavior:
      * Persists ``status = new_status`` via ``update_fields``.
        Event-opening notifications are emitted by the ``post_save`` signal
        in ``events/signals.py``; we do not call them directly.
      * On transition to CANCELLED, persists ``cancellation_reason`` (an
        empty string when none is supplied) and revokes all PENDING
        ``WaitlistOffer``s for this event — outstanding offers are
        meaningless once the event is gone.
      * On transition AWAY from CANCELLED (un-cancel), clears any stale
        ``cancellation_reason`` (it describes a specific cancellation) and
        enqueues a waitlist processing pass so the freshly real seats can be
        taken by waitlisted users.

    Args:
        event: The event to mutate.
        new_status: The target status.
        cancellation_reason: Optional organizer-supplied reason, honored only
            when ``new_status`` is CANCELLED; ignored for other transitions.

    Returns:
        The updated ``Event`` (same instance).
    """
    old_status = event.status
    update_fields = ["status"]
    event.status = new_status

    if new_status == models.Event.EventStatus.CANCELLED:
        event.cancellation_reason = cancellation_reason or ""
        update_fields.append("cancellation_reason")
    elif old_status == models.Event.EventStatus.CANCELLED:
        # The reason described the prior cancellation; don't let it resurrect.
        event.cancellation_reason = ""
        update_fields.append("cancellation_reason")

    event.save(update_fields=update_fields)

    if new_status == models.Event.EventStatus.CANCELLED:
        revoke_all_pending_offers(event.id)
    elif old_status == models.Event.EventStatus.CANCELLED:
        enqueue_waitlist_processing(event.id)

    return event


@transaction.atomic
def update_slug(event: models.Event, slug: str) -> models.Event:
    """Rename an event's slug, enforcing per-organization uniqueness.

    Args:
        event: The event whose slug should change.
        slug: The new slug (already validated by the schema for format).

    Returns:
        The updated ``Event`` (same instance).

    Raises:
        SlugAlreadyExistsError: If another event in the same organization
            already uses this slug.
    """
    if models.Event.objects.filter(organization_id=event.organization_id, slug=slug).exclude(pk=event.pk).exists():
        raise SlugAlreadyExistsError(slug)

    event.slug = slug
    event.save(update_fields=["slug"])
    return event


def update_event_schedule(event: models.Event, sessions: list[EventScheduleSession]) -> models.Event:
    """Replace an event's schedule with the provided sessions (full-array replace).

    Args:
        event: The event to update.
        sessions: Validated schedule sessions (order preserved as authored).

    Returns:
        The updated event.
    """
    event.schedule = [s.model_dump(mode="json") for s in sessions]
    event.save(update_fields=["schedule", "updated_at"])  # full_clean re-validates via clean()
    return event


# Translated message used by the controller when surfacing
# ``SlugAlreadyExistsError`` to clients. Kept here so the service owns the
# canonical wording while the controller only does HTTP mapping.
SLUG_ALREADY_EXISTS_MESSAGE = _("An event with this slug already exists in your organization.")
