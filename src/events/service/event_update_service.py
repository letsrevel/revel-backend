"""Event update service.

Owns the side-effecting workflows for creating and editing an event:

- ``create_event``: creates the event, then links any inline
  ``series_pass_links`` (see ``update_event``'s note on the same field).
- ``update_event``: applies an ``EventEditSchema`` payload, marks series
  occurrences as ``is_modified`` when a real field change occurs, and
  triggers waitlist side effects on capacity / waitlist-open transitions.
- ``update_status``: applies a status transition and dispatches the
  matching waitlist side effects (revoke pending offers on CANCELLED,
  re-process waitlist on un-cancel).
- ``update_slug``: enforces per-organization slug uniqueness and persists
  the new slug.
- ``update_event_schedule``: replaces the event's display-only schedule
  (full-array replace of the relative-offset session list).

The controller layer (``events/controllers/event_admin/core.py``) must not
read/diff/dispatch around these flows directly; it should call into this
module so all the side effects stay in one place.
"""

import typing as t

from django.db import transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from accounts.models import RevelUser
from common.utils import update_db_instance
from events import models
from events.exceptions import EventRefundsStartedError
from events.schema import EventCreateSchema, EventEditSchema, SeriesPassLinkInputSchema
from events.service import series_pass_service
from events.service.waitlist_service import enqueue_waitlist_processing, revoke_all_pending_offers
from events.utils.schedule import EventScheduleSession
from events.utils.visibility_settings import build_visibility_settings_update, validate_visibility_settings


class SlugAlreadyExistsError(Exception):
    """Raised when an event slug update collides with an existing event in the same org."""

    def __init__(self, slug: str) -> None:
        """Initialise the error with the slug that collided."""
        super().__init__(f"Event slug '{slug}' already exists in this organization.")
        self.slug = slug


def _link_series_passes(event: models.Event, links: list[SeriesPassLinkInputSchema] | None) -> None:
    """Create a ``SeriesPassTierLink`` for each requested (series pass, tier) pair.

    Each entry covers ``event`` via ``tier_id``, using one ``add_tier_links`` call per
    pass so the coverage gate and materialization dispatch stay scoped per pass. The
    ``event_series`` filter on the ``SeriesPass`` lookup 404s a pass from the wrong
    series (or any pass at all, when ``event`` has no series) before the coverage gate
    or model validation ever runs.

    Args:
        event: The event the links attach to (already persisted, with its final
            ``event_series``).
        links: The requested links, or ``None``/empty when the client omitted the field.

    Raises:
        django.http.Http404: If a ``series_pass_id`` doesn't resolve to a ``SeriesPass``
            on ``event.event_series``.
        events.exceptions.SeriesPassCoverageError: If ``event`` fails the coverage gate.
        django.core.exceptions.ValidationError: If ``tier_id`` fails model validation.
    """
    if not links:
        return
    for link in links:
        # Filter by ``event_series_id`` (not the ``event_series`` FK descriptor): the
        # latter can serve a stale cached object after ``update_db_instance`` changes
        # ``event_series_id`` via a plain ``setattr``, which doesn't invalidate Django's
        # forward-FK cache.
        series_pass = get_object_or_404(
            models.SeriesPass, pk=link.series_pass_id, event_series_id=event.event_series_id
        )
        series_pass_service.add_tier_links(series_pass, [{"event_id": event.id, "tier_id": link.tier_id}])


def _field_changed(field: str, before: t.Any, after: t.Any) -> bool:
    """Whether an edited field's value actually differs, for the occurrence diff.

    A plain ``!=`` is right for the scalar types ``EventEditSchema`` exposes, but
    wrong for ``visibility_settings``: it is a JSON blob whose empty form and its
    explicitly-spelled-out all-defaults form mean the same thing. Comparing the
    raw dicts would report ``{}`` → ``{"show_capacity": true}`` as a change and
    flip ``is_modified=True``, permanently detaching the occurrence from template
    propagation over an edit that changed nothing. Frontends that round-trip the
    whole settings object on every save would trip this on the first edit.

    Args:
        field: The field name being compared.
        before: The pre-update value.
        after: The post-update value.

    Returns:
        True when the values differ in meaning, not merely in representation.
    """
    if field == "visibility_settings":
        return validate_visibility_settings(before) != validate_visibility_settings(after)
    return bool(before != after)


@transaction.atomic
def create_event(organization: models.Organization, payload: EventCreateSchema) -> models.Event:
    """Create an event and link any inline ``series_pass_links``.

    Args:
        organization: The organization the event belongs to.
        payload: The validated create payload.

    Returns:
        The newly created ``Event``.
    """
    create_kwargs = payload.model_dump(exclude={"series_pass_links"})
    # A plain dump keeps datetimes as ``datetime`` objects, which
    # ``Event.objects.create`` needs — but it also leaves any enum member
    # inside ``visibility_settings`` (e.g. ``address_visibility``) un-stringified.
    # Overwrite just that key with a json-mode dump so the in-memory instance
    # never holds a ``ResourceVisibility`` member, mirroring the edit path's
    # ``build_visibility_settings_update``.
    create_kwargs["visibility_settings"] = payload.visibility_settings.model_dump(mode="json")
    event = models.Event.objects.create(organization=organization, **create_kwargs)
    _link_series_passes(event, payload.series_pass_links)
    return event


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

    # Merge partial visibility toggles onto the stored blob rather than replacing
    # it: naming one toggle must not silently re-enable the ones left out.
    updated_event = update_db_instance(
        event,
        payload,
        exclude={"series_pass_links"},
        **build_visibility_settings_update(event.visibility_settings, payload),
    )
    _link_series_passes(updated_event, payload.series_pass_links)

    # Mark occurrences as modified only when a persisted field actually
    # changed. Comparing against the pre-update snapshot avoids marking
    # idempotent no-op PUTs as manual edits.
    #
    # The ``!=`` comparison is reliable for the simple types
    # ``EventEditSchema`` actually exposes (str, int, bool, datetime).
    # GIS Point objects or cross-tz datetimes could compare unequal for
    # structurally-identical values, but neither is exposed here.
    # ``visibility_settings`` is the one exception and is normalized — see
    # ``_field_changed``.
    if track_is_modified and pre_values:
        changed = any(_field_changed(field, pre_values[field], getattr(updated_event, field)) for field in pre_values)
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
    refund_tickets: bool = False,
    initiated_by: RevelUser | None = None,
) -> models.Event:
    """Transition an event's status and fire matching waitlist/refund side effects.

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
        taken by waitlisted users. Raises ``EventRefundsStartedError`` if the
        bulk refund sweep already started — that process is irreversible.
      * On transition to CANCELLED with ``refund_tickets=True``, ALWAYS
        dispatches the bulk cancel-and-refund sweep on commit —
        ``tickets_refund_started_at`` is stamped only the first time (a no-op
        if already set). The sweep itself is idempotent per ticket, so
        re-POSTing ``update-status/cancelled`` with the flag (e.g. after a
        crashed/partial run) is the supported way to resume it.
      * Locks the event row (``select_for_update``) before reading/stamping
        ``tickets_refund_started_at`` so two concurrent calls can't both
        observe the un-set stamp and race on the un-cancel guard / dispatch.

    Requires an active transaction: the function re-fetches ``event`` under
    ``select_for_update()`` as its first step, which raises
    ``TransactionManagementError`` outside one. The ``@transaction.atomic``
    decorator on this function provides it, so a direct call is always safe;
    it also means every write here (or a rollback) is scoped to that block,
    not the caller's.

    Args:
        event: The event to mutate. Only its ``pk`` is used — the instance
            itself is discarded in favor of a freshly locked re-fetch (see
            Returns).
        new_status: The target status.
        cancellation_reason: Optional organizer-supplied reason, honored only
            when ``new_status`` is CANCELLED; ignored for other transitions.
        refund_tickets: Only honored when ``new_status`` is CANCELLED: cancel
            every ticket and refund online payments in the background.
        initiated_by: The user requesting the transition, threaded through to
            the refund sweep as the acting user. Ignored unless
            ``refund_tickets`` triggers a dispatch.

    Returns:
        The updated ``Event`` — a distinct, freshly re-fetched instance
        (``select_for_update().get(pk=event.pk)``), not the ``event`` object
        the caller passed in.

    Raises:
        EventRefundsStartedError: On an un-cancel attempt after the bulk
            refund sweep already started for this event.
    """
    event = models.Event.objects.select_for_update().get(pk=event.pk)
    old_status = event.status
    if old_status == models.Event.EventStatus.CANCELLED and new_status != models.Event.EventStatus.CANCELLED:
        if event.tickets_refund_started_at is not None:
            raise EventRefundsStartedError()

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

    if new_status == models.Event.EventStatus.CANCELLED and refund_tickets:
        if event.tickets_refund_started_at is None:
            event.tickets_refund_started_at = timezone.now()
            event.save(update_fields=["tickets_refund_started_at"])
        event_id, actor_id = str(event.id), str(initiated_by.id) if initiated_by else None

        def _dispatch_refund_sweep() -> None:
            from events.tasks.refunds import refund_cancelled_event_tickets

            refund_cancelled_event_tickets.delay(event_id, actor_id)

        # ATOMIC_REQUESTS: never .delay() inside the request transaction.
        # Always dispatch (not gated on the stamp): the parent fans out
        # per-ticket subtasks that are individually idempotent, so
        # re-dispatching is the supported resume path for a partial run.
        transaction.on_commit(_dispatch_refund_sweep)

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


@transaction.atomic
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
