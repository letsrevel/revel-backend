"""Controller for recurring event management."""

from uuid import UUID

from django.db import transaction
from django.shortcuts import get_object_or_404
from ninja_extra import api_controller, route

from common.authentication import I18nJWTAuth
from common.schema import ValidationErrorResponse
from common.throttling import WriteThrottle
from events import models, schema
from events.controllers.permissions import OrganizationPermission
from events.service import recurrence_service, update_db_instance

from .base import OrganizationAdminBaseController


@api_controller("/organization-admin/{slug}", auth=I18nJWTAuth(), tags=["Organization Admin"], throttle=WriteThrottle())
class OrganizationAdminRecurringEventsController(OrganizationAdminBaseController):
    """Recurring event management endpoints.

    Handles creation of recurring events, template editing with propagation,
    recurrence rule updates, occurrence cancellation, manual generation,
    and pause/resume.
    """

    def _get_series(self, slug: str, series_id: UUID) -> models.EventSeries:
        """Fetch series and validate it belongs to the organization."""
        organization = self.get_one(slug)
        return get_object_or_404(
            models.EventSeries.objects.select_related("recurrence_rule", "template_event"),
            id=series_id,
            organization=organization,
        )

    @route.post(
        "/create-recurring-event",
        url_name="create_recurring_event",
        response={200: schema.EventSeriesRecurrenceDetailSchema, 400: ValidationErrorResponse},
        permissions=[OrganizationPermission("create_event")],
    )
    @transaction.atomic
    def create_recurring_event(self, slug: str, payload: schema.RecurringEventCreateSchema) -> models.EventSeries:
        """Create a recurring event series with template and initial generation.

        Creates: RecurrenceRule + EventSeries + template Event, then materializes
        events within the configured rolling window.
        """
        organization = self.get_one(slug)

        # Create recurrence rule
        rule = models.RecurrenceRule(**payload.recurrence.model_dump())
        rule.full_clean()
        rule.save()

        # Create series
        series = models.EventSeries.objects.create(
            organization=organization,
            name=payload.series_name,
            description=payload.series_description,
            recurrence_rule=rule,
            auto_publish=payload.auto_publish,
            generation_window_weeks=payload.generation_window_weeks,
        )

        # Create template event (exclude event_series_id — we set it explicitly)
        event_data = payload.event.model_dump(exclude={"event_series_id"})
        template_event = models.Event(
            organization=organization,
            event_series=series,
            is_template=True,
            **event_data,
        )
        template_event.save()

        series.template_event = template_event
        series.save(update_fields=["template_event"])

        # Generate initial events
        recurrence_service.generate_series_events(series)

        series.refresh_from_db()
        return series

    @route.patch(
        "/event-series/{series_id}/template",
        url_name="update_series_template",
        response={200: schema.EventSeriesRecurrenceDetailSchema, 400: ValidationErrorResponse},
        permissions=[OrganizationPermission("edit_event_series")],
    )
    def update_template(
        self,
        slug: str,
        series_id: UUID,
        payload: schema.EventEditSchema,
        propagate: str = "none",
    ) -> models.EventSeries:
        """Update the series template event, optionally propagating to future occurrences.

        Use the `propagate` query parameter:
        - "none" (default): only the template is updated; next generation picks up changes.
        - "future_unmodified": update future occurrences that haven't been manually edited.
        - "all_future": update all future occurrences, including manually edited ones.

        Only safe fields are propagated (name, description, visibility, etc.).
        Date/time, status, and FK fields are per-occurrence and never propagated.
        """
        series = self._get_series(slug, series_id)
        if not series.template_event:
            from ninja.errors import HttpError

            raise HttpError(400, "Series has no template event.")

        changed_data = payload.model_dump(exclude_unset=True)
        update_db_instance(series.template_event, payload)

        if propagate != "none" and changed_data:
            recurrence_service.propagate_template_changes(series, changed_data, scope=propagate)

        series.refresh_from_db()
        return series

    @route.patch(
        "/event-series/{series_id}/recurrence",
        url_name="update_series_recurrence",
        response={200: schema.EventSeriesRecurrenceDetailSchema, 400: ValidationErrorResponse},
        permissions=[OrganizationPermission("edit_event_series")],
    )
    def update_recurrence(
        self, slug: str, series_id: UUID, payload: schema.EventSeriesRecurrenceUpdateSchema
    ) -> models.EventSeries:
        """Update recurrence rule and/or series settings."""
        series = self._get_series(slug, series_id)

        # Update series-level fields
        series_fields: list[str] = []
        if payload.auto_publish is not None:
            series.auto_publish = payload.auto_publish
            series_fields.append("auto_publish")
        if payload.generation_window_weeks is not None:
            series.generation_window_weeks = payload.generation_window_weeks
            series_fields.append("generation_window_weeks")
        if series_fields:
            series.save(update_fields=series_fields)

        # Update recurrence rule fields
        if payload.recurrence and series.recurrence_rule:
            rule_data = payload.recurrence.model_dump(exclude_unset=True)
            if rule_data:
                for field, value in rule_data.items():
                    setattr(series.recurrence_rule, field, value)
                series.recurrence_rule.full_clean()
                series.recurrence_rule.save()

        series.refresh_from_db()
        return series

    @route.post(
        "/event-series/{series_id}/cancel-occurrence",
        url_name="cancel_series_occurrence",
        response={200: schema.EventSeriesRecurrenceDetailSchema},
        permissions=[OrganizationPermission("edit_event_series")],
    )
    def cancel_occurrence(
        self, slug: str, series_id: UUID, payload: schema.CancelOccurrenceSchema
    ) -> models.EventSeries:
        """Cancel a single occurrence. Adds to exdates and cancels materialized event if present."""
        series = self._get_series(slug, series_id)
        recurrence_service.cancel_occurrence(series, payload.occurrence_date)
        series.refresh_from_db()
        return series

    @route.post(
        "/event-series/{series_id}/generate",
        url_name="generate_series_events",
        response=list[schema.EventDetailSchema],
        permissions=[OrganizationPermission("edit_event_series")],
    )
    def generate_events(
        self,
        slug: str,
        series_id: UUID,
        payload: schema.GenerateSeriesEventsSchema | None = None,
    ) -> list[models.Event]:
        """Manually generate events for the series within the rolling window."""
        series = self._get_series(slug, series_id)
        until = payload.until if payload else None
        return recurrence_service.generate_series_events(series, until_override=until)

    @route.post(
        "/event-series/{series_id}/pause",
        url_name="pause_series",
        response=schema.EventSeriesRecurrenceDetailSchema,
        permissions=[OrganizationPermission("edit_event_series")],
    )
    def pause_series(self, slug: str, series_id: UUID) -> models.EventSeries:
        """Pause generation without cancelling existing events."""
        series = self._get_series(slug, series_id)
        recurrence_service.pause_series(series)
        series.refresh_from_db()
        return series

    @route.post(
        "/event-series/{series_id}/resume",
        url_name="resume_series",
        response=schema.EventSeriesRecurrenceDetailSchema,
        permissions=[OrganizationPermission("edit_event_series")],
    )
    def resume_series(self, slug: str, series_id: UUID) -> models.EventSeries:
        """Resume generation for a paused series."""
        series = self._get_series(slug, series_id)
        recurrence_service.resume_series(series)
        series.refresh_from_db()
        return series
