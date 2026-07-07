"""Series pass service: enable-time coverage gate and tier link creation.

Quotes, materialization dispatch, and cancellation land in later tasks of the
series-passes plan (issue #644).
"""

import typing as t
from uuid import UUID

from django.db import transaction
from django.db.models import Q
from django.utils.translation import gettext_lazy as _

from events.exceptions import SeriesPassCoverageError
from events.models import Event, EventSeries, OrganizationQuestionnaire, SeriesPass, SeriesPassTierLink


class TierLinkInput(t.TypedDict):
    """One (event, tier) pair to link to a SeriesPass."""

    event_id: UUID
    tier_id: UUID


def validate_events_coverable(series: EventSeries, events: t.Sequence[Event]) -> None:
    """Enforce the enable-time coverage gate for a series pass.

    Every event covered by a series pass must be "simple": it must belong to
    the given series (which must itself be non-recurring), be OPEN, require a
    ticket, not be invitation-only, and not be gated by an admission
    questionnaire targeting either the event or the series.

    Args:
        series: The EventSeries the pass belongs to.
        events: The events the pass is meant to cover.

    Raises:
        SeriesPassCoverageError: If the series is recurring, or any event
            fails the coverage gate.
    """
    if series.recurrence_rule_id is not None:
        raise SeriesPassCoverageError(str(_("Series passes are not supported on recurring series.")))
    for event in events:
        if event.event_series_id != series.id:
            raise SeriesPassCoverageError(str(_("Event '%s' does not belong to this series.") % event.name))
        if event.status != Event.EventStatus.OPEN:
            raise SeriesPassCoverageError(str(_("Event '%s' is not open.") % event.name))
        if not event.requires_ticket:
            raise SeriesPassCoverageError(str(_("Event '%s' does not require a ticket.") % event.name))
        if event.visibility == Event.Visibility.PRIVATE:
            raise SeriesPassCoverageError(
                str(_("Event '%s' is invitation-only and cannot be covered by a series pass.") % event.name)
            )
    gated = OrganizationQuestionnaire.objects.filter(
        questionnaire_type=OrganizationQuestionnaire.QuestionnaireType.ADMISSION,
    ).filter(Q(event_series=series) | Q(events__in=[event.pk for event in events]))
    if gated.exists():
        raise SeriesPassCoverageError(
            str(_("Events gated by an admission questionnaire cannot be covered by a series pass."))
        )


@transaction.atomic
def add_tier_links(series_pass: SeriesPass, links: list[TierLinkInput]) -> list[SeriesPassTierLink]:
    """Create and full-clean tier links for a series pass, after the coverage gate.

    Args:
        series_pass: The SeriesPass to attach links to.
        links: Event/tier id pairs to link.

    Returns:
        The created ``SeriesPassTierLink`` instances, in input order.

    Raises:
        SeriesPassCoverageError: If any covered event fails the coverage gate.
        django.core.exceptions.ValidationError: If a link fails model-level
            validation (tier/event/series/currency/seat-mode mismatch).
    """
    events = list(Event.objects.filter(pk__in=[link["event_id"] for link in links]))
    validate_events_coverable(series_pass.event_series, events)
    created: list[SeriesPassTierLink] = []
    for link in links:
        tier_link = SeriesPassTierLink(series_pass=series_pass, event_id=link["event_id"], tier_id=link["tier_id"])
        tier_link.full_clean()
        tier_link.save()
        created.append(tier_link)
    return created


def create_series_pass(
    series: EventSeries,
    payload: t.Any,  # ponytail: typed as t.Any until SeriesPassCreateSchema lands in Task 15
) -> SeriesPass:
    """Create a SeriesPass and its tier links in a single transaction.

    Args:
        series: The EventSeries the pass belongs to.
        payload: A ``SeriesPassCreateSchema``-shaped object exposing
            ``model_dump(exclude={"tier_links"})`` for the pass fields and
            ``tier_links_as_inputs`` (``list[TierLinkInput]``) for the links.

    Returns:
        The created SeriesPass with its tier links attached.

    Raises:
        SeriesPassCoverageError: If the series is recurring or a covered
            event fails the coverage gate.
        django.core.exceptions.ValidationError: If the pass or a tier link
            fails model validation.
    """
    with transaction.atomic():
        series_pass = SeriesPass(event_series=series, **payload.model_dump(exclude={"tier_links"}))
        series_pass.full_clean()
        series_pass.save()
        add_tier_links(series_pass, payload.tier_links_as_inputs)
    return series_pass
