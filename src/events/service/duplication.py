"""Event duplication service."""

import typing as t
from datetime import datetime

from django.db import transaction

from events.models import Event, PotluckItem, TicketTier
from events.suppression import suppress_default_tier_creation

# Fields that are NOT copied from the template. These fall into three groups:
# 1. Primary key / timestamps — auto-managed by Django.
# 2. Slug — regenerated via SlugFromNameMixin based on the new name.
# 3. Per-occurrence state that must never leak from template to occurrence
#    (is_template, is_modified, attendee_count, auto-generated thumbnails).
# 4. Fields explicitly overridden or shifted by this function
#    (name, status, start, end, occurrence_index, date windows).
_EXCLUDED_FROM_COPY: frozenset[str] = frozenset(
    {
        # primary key / timestamps
        "id",
        "created_at",
        "updated_at",
        # auto-generated
        "slug",
        "logo_thumbnail",
        "cover_art_thumbnail",
        "cover_art_social",
        # per-occurrence state
        "attendee_count",
        "is_template",
        "is_modified",
        # explicit overrides
        "name",
        "status",
        "occurrence_index",
        # date fields handled separately (shifted)
        "start",
        "end",
        "rsvp_before",
        "apply_before",
        "check_in_starts_at",
        "check_in_ends_at",
    }
)

_SHIFTED_DATE_FIELDS: tuple[str, ...] = (
    "rsvp_before",
    "apply_before",
    "check_in_starts_at",
    "check_in_ends_at",
)


def _collect_copyable_field_values(template_event: Event) -> dict[str, t.Any]:
    """Collect every copyable concrete field value from the template event.

    Iterating ``_meta.concrete_fields`` with an explicit exclusion list ensures
    that new fields added to Event are copied by default. Fields that must
    never be copied (per-occurrence state, overrides) live in
    ``_EXCLUDED_FROM_COPY``. Concrete fields are exactly the columns backed by
    the database, so reverse relations, M2M, and GenericRelations are excluded
    automatically.

    Returns:
        Mapping of ``attname`` to value, ready to splat into ``Event.objects.create``.
    """
    values: dict[str, t.Any] = {}
    for field in template_event._meta.concrete_fields:
        if field.name in _EXCLUDED_FROM_COPY:
            continue
        # Use attname for FKs (e.g. organization_id) to avoid loading related obj.
        values[field.attname] = getattr(template_event, field.attname)
    return values


@transaction.atomic
def duplicate_event(
    template_event: Event,
    new_name: str,
    new_start: datetime,
    *,
    occurrence_index: int | None = None,
    status_override: Event.EventStatus | None = None,
) -> Event:
    """Create a deep copy of an event with shifted dates.

    All date fields are shifted by the delta between template_event.start and new_start.
    The new event is created in DRAFT status unless ``status_override`` is provided.

    Copies:
    - All scalar and FK event fields (iterated from ``Event._meta`` with an
      explicit exclusion list — new Event fields are copied automatically).
    - Ticket tiers (with date shifts, reset quantity_sold)
    - Potluck items (without assignments; all become host suggestions)
    - Tags

    Links to same:
    - Organization questionnaires (M2M)
    - Additional resources (M2M)

    Does NOT copy:
    - Tickets, RSVPs, invitations, tokens, waitlist
    - Potluck item assignees/creators
    - Per-occurrence state (is_template, is_modified, attendee_count)
    - Auto-generated thumbnails

    Args:
        template_event: Event to copy from
        new_name: Name for new event
        new_start: Start datetime (anchor for all date shifts)
        occurrence_index: If provided, set on the new event (used by recurring series
            materialization to stamp a monotonic index).
        status_override: If provided, use this status instead of the default DRAFT
            (used by recurring series with auto_publish=True).

    Returns:
        New Event. Status is DRAFT unless ``status_override`` is provided.
    """
    delta = new_start - template_event.start

    def shift_date(dt: datetime | None) -> datetime | None:
        return dt + delta if dt else None

    # Collect all copyable fields from the template via model introspection.
    copy_kwargs = _collect_copyable_field_values(template_event)

    # Apply shifted date fields.
    for field_name in _SHIFTED_DATE_FIELDS:
        copy_kwargs[field_name] = shift_date(getattr(template_event, field_name))

    # Apply explicit overrides.
    status = status_override if status_override is not None else Event.EventStatus.DRAFT
    copy_kwargs["name"] = new_name
    copy_kwargs["status"] = status
    copy_kwargs["occurrence_index"] = occurrence_index
    copy_kwargs["start"] = new_start
    copy_kwargs["end"] = template_event.end + delta
    # New events are never templates; is_modified resets for each occurrence.
    copy_kwargs["is_template"] = False
    copy_kwargs["is_modified"] = False

    # Suppress auto-creation of default ticket tier (we copy tiers from template)
    with suppress_default_tier_creation():
        new_event = Event.objects.create(**copy_kwargs)

    _duplicate_ticket_tiers(template_event, new_event, shift_date)
    _duplicate_potluck_items(template_event, new_event)
    _link_m2m_relations(template_event, new_event)

    return new_event


def _duplicate_ticket_tiers(
    template_event: Event,
    new_event: Event,
    shift_date: t.Callable[[datetime | None], datetime | None],
) -> None:
    """Duplicate ticket tiers from template, resetting quantity_sold."""
    for tier in template_event.ticket_tiers.all():
        new_tier = TicketTier.objects.create(
            event=new_event,
            name=tier.name,
            visibility=tier.visibility,
            payment_method=tier.payment_method,
            purchasable_by=tier.purchasable_by,
            description=tier.description,
            price=tier.price,
            price_type=tier.price_type,
            pwyc_min=tier.pwyc_min,
            pwyc_max=tier.pwyc_max,
            currency=tier.currency,
            total_quantity=tier.total_quantity,
            quantity_sold=0,
            manual_payment_instructions=tier.manual_payment_instructions,
            sales_start_at=shift_date(tier.sales_start_at),
            sales_end_at=shift_date(tier.sales_end_at),
        )
        new_tier.restricted_to_membership_tiers.set(tier.restricted_to_membership_tiers.all())


def _duplicate_potluck_items(template_event: Event, new_event: Event) -> None:
    """Duplicate all potluck items as host suggestions, without assignments."""
    items = [
        PotluckItem(
            event=new_event,
            name=item.name,
            quantity=item.quantity,
            item_type=item.item_type,
            note=item.note,
            is_suggested=True,
            created_by=None,
            assignee=None,
        )
        for item in template_event.potluck_items.all()
    ]
    if items:
        PotluckItem.objects.bulk_create(items)


def _link_m2m_relations(template_event: Event, new_event: Event) -> None:
    """Copy tags, questionnaires, and additional resources from template."""
    tag_names = [tag.name for tag in template_event.tags_manager.all()]
    if tag_names:
        new_event.tags_manager.add(*tag_names)

    for oq in template_event.org_questionnaires.all():
        oq.events.add(new_event)

    for resource in template_event.additional_resources.all():
        resource.events.add(new_event)
