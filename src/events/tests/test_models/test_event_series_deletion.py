"""Lock in EventSeries deletion semantics for the recurring-events feature.

The ``EventSeries.template_event`` and ``EventSeries.recurrence_rule``
foreign keys use ``on_delete=PROTECT`` so that an admin DELETE click on a
template Event or RecurrenceRule cannot silently destroy a paid-ticket-
bearing series. Series teardown must always go through the service layer
(or, in the absence of that, through deletion of the EventSeries itself,
which then cascades to its events via ``Event.event_series=CASCADE``).

These tests pin that contract so a future change to the on_delete value
breaks something visible in CI.
"""

from datetime import timedelta

import pytest
from django.db.models import ProtectedError
from django.utils import timezone

from events.models import Event, EventSeries, Organization, RecurrenceRule

pytestmark = pytest.mark.django_db


def _build_series_with_template_and_rule(organization: Organization) -> EventSeries:
    """Build an EventSeries wired to a template Event and a RecurrenceRule."""
    dtstart = timezone.now() + timedelta(days=1)
    rule = RecurrenceRule.objects.create(
        frequency=RecurrenceRule.Frequency.WEEKLY,
        interval=1,
        weekdays=[0],
        dtstart=dtstart,
    )
    series = EventSeries.objects.create(
        organization=organization,
        name="Protected Series",
        recurrence_rule=rule,
    )
    template = Event.objects.create(
        organization=organization,
        event_series=series,
        name="Protected Series Template",
        start=dtstart,
        end=dtstart + timedelta(hours=2),
        status=Event.EventStatus.DRAFT,
        visibility=Event.Visibility.PUBLIC,
        event_type=Event.EventType.PUBLIC,
        is_template=True,
    )
    series.template_event = template
    series.save(update_fields=["template_event"])
    return series


class TestEventSeriesProtectedDeletion:
    """Direct deletion of a template/rule must be refused while a series points to it."""

    def test_deleting_template_event_directly_is_protected(
        self,
        organization: Organization,
    ) -> None:
        """Deleting the template Event must raise ``ProtectedError``.

        This is the critical safety property: an admin clicking DELETE on
        the template event in /admin/events/event/ must NOT cascade-destroy
        the entire series and its tickets/RSVPs/attendees. ``PROTECT``
        forces the operator to delete the series instead, which goes
        through Event.event_series=CASCADE in a controlled way.
        """
        series = _build_series_with_template_and_rule(organization)
        template = series.template_event
        assert template is not None

        with pytest.raises(ProtectedError):
            template.delete()

        # The series and template are still intact.
        series.refresh_from_db()
        template.refresh_from_db()
        assert series.template_event_id == template.id

    def test_deleting_recurrence_rule_directly_is_protected(
        self,
        organization: Organization,
    ) -> None:
        """Deleting the RecurrenceRule must raise ``ProtectedError``.

        Less catastrophic than deleting the template (rules don't carry
        tickets), but the same principle applies: a series with a missing
        rule is a generation-broken zombie. Force the operator to go
        through the series-level teardown path.
        """
        series = _build_series_with_template_and_rule(organization)
        rule = series.recurrence_rule
        assert rule is not None

        with pytest.raises(ProtectedError):
            rule.delete()

        series.refresh_from_db()
        rule.refresh_from_db()
        assert series.recurrence_rule_id == rule.id

    def test_deleting_series_cascades_to_template_and_occurrences(
        self,
        organization: Organization,
    ) -> None:
        """Deleting the EventSeries cascades to all its events including the template.

        ``PROTECT`` only blocks deletion of the *referenced* row (template
        or rule). Deleting the EventSeries itself is unaffected: the
        ``Event.event_series`` FK is ``CASCADE``, so all events (including
        the template) are removed in one go. The recurrence_rule is left
        orphaned by design — cleanup of the rule belongs in a service-level
        teardown helper if/when one is added.
        """
        series = _build_series_with_template_and_rule(organization)
        template = series.template_event
        rule = series.recurrence_rule
        assert template is not None
        assert rule is not None
        template_id = template.id
        rule_id = rule.id

        # Add a regular (non-template) occurrence so we can verify it
        # cascades alongside the template.
        occurrence = Event.objects.create(
            organization=organization,
            event_series=series,
            name="Series Occurrence",
            start=template.start + timedelta(days=7),
            end=template.end + timedelta(days=7),
            status=Event.EventStatus.OPEN,
            visibility=Event.Visibility.PUBLIC,
            event_type=Event.EventType.PUBLIC,
        )

        series.delete()

        assert not Event.objects.filter(id=template_id).exists()
        assert not Event.objects.filter(id=occurrence.id).exists()
        # The orphaned rule is *not* deleted by the cascade — this is the
        # known trade-off of PROTECT. If a service-level cleanup is added,
        # update this assertion accordingly.
        assert RecurrenceRule.objects.filter(id=rule_id).exists()
