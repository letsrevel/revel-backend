"""Controller for recurring event management."""

from uuid import UUID

from django.shortcuts import get_object_or_404
from ninja import Body
from ninja.errors import HttpError
from ninja_extra import api_controller, route

from common.authentication import I18nJWTAuth
from common.schema import ValidationErrorResponse
from common.throttling import WriteThrottle
from events import models, schema
from events.controllers.permissions import OrganizationPermission
from events.service import recurrence_service
from events.service.recurrence_service import PropagateScope

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
            models.EventSeries.objects.select_related(
                "recurrence_rule",
                "template_event",
                "template_event__venue",
            ),
            id=series_id,
            organization=organization,
        )

    @route.post(
        "/create-recurring-event",
        url_name="create_recurring_event",
        response={201: schema.EventSeriesRecurrenceDetailSchema, 400: ValidationErrorResponse},
        permissions=[OrganizationPermission("create_event")],
    )
    def create_recurring_event(
        self, slug: str, payload: schema.RecurringEventCreateSchema
    ) -> tuple[int, models.EventSeries]:
        """Create a recurring event series with template and initial generation."""
        organization = self.get_one(slug)
        series = recurrence_service.create_recurring_event_series(
            organization,
            recurrence_data=payload.recurrence.model_dump(),
            series_name=payload.series_name,
            series_description=payload.series_description,
            auto_publish=payload.auto_publish,
            generation_window_weeks=payload.generation_window_weeks,
            event_data=payload.event.model_dump(exclude={"event_series_id"}),
        )
        return 201, series

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
        payload: schema.TemplateEditSchema,
        propagate: PropagateScope = PropagateScope.NONE,
    ) -> models.EventSeries:
        """Update the series template event, optionally propagating to future occurrences.

        Use the `propagate` query parameter:
        - "none" (default): only the template is updated; next generation picks up changes.
        - "future_unmodified": update future occurrences that haven't been manually edited.
        - "all_future": update all future occurrences, including manually edited ones.

        Only safe fields are propagated (name, description, visibility, etc.).
        Date/time, status, FK, and slug fields are per-occurrence and never propagated.
        The template is bound to its series and venue at creation time; use dedicated
        endpoints if those associations must change.
        """
        series = self._get_series(slug, series_id)
        try:
            return recurrence_service.update_template(series, payload, scope=propagate)
        except ValueError as exc:
            raise HttpError(400, str(exc)) from exc

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
        recurrence_data = payload.recurrence.model_dump(exclude_unset=True) if payload.recurrence else None
        return recurrence_service.update_series_recurrence(
            series,
            auto_publish=payload.auto_publish,
            generation_window_weeks=payload.generation_window_weeks,
            recurrence_data=recurrence_data,
        )

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
        payload: schema.GenerateSeriesEventsSchema | None = Body(None),  # type: ignore[type-arg]
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
