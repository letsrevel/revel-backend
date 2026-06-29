import typing as t
from uuid import UUID

from django.shortcuts import get_object_or_404
from ninja_extra import route

from events import models as event_models
from events import schema as event_schema
from events.service import event_questionnaire_service

from ..permissions import QuestionnairePermission
from .base import QuestionnaireControllerBase


class QuestionnaireAssignmentsMixin(QuestionnaireControllerBase):
    """Assign/unassign questionnaires to events and event series."""

    @route.put(
        "/{org_questionnaire_id}/events",
        url_name="replace_questionnaire_events",
        response=event_schema.OrganizationQuestionnaireSchema,
        permissions=[QuestionnairePermission("edit_questionnaire")],
    )
    def replace_events(
        self, org_questionnaire_id: UUID, payload: event_schema.EventAssignmentSchema
    ) -> event_models.OrganizationQuestionnaire:
        """Replace all assigned events for this questionnaire (admin only).

        Batch operation to set exactly which events require this questionnaire. Validates that
        events belong to the same organization. Requires 'edit_questionnaire' permission.
        """
        org_questionnaire = t.cast(
            event_models.OrganizationQuestionnaire,
            self.get_object_or_exception(self.get_queryset(), pk=org_questionnaire_id),
        )
        return event_questionnaire_service.replace_events(org_questionnaire, payload.event_ids)

    @route.post(
        "/{org_questionnaire_id}/events/{event_id}",
        url_name="assign_questionnaire_event",
        response=event_schema.OrganizationQuestionnaireSchema,
        permissions=[QuestionnairePermission("edit_questionnaire")],
    )
    def assign_event(self, org_questionnaire_id: UUID, event_id: UUID) -> event_models.OrganizationQuestionnaire:
        """Assign a single event to this questionnaire (admin only).

        Adds one event that will require completion of this questionnaire. Requires
        'edit_questionnaire' permission.
        """
        org_questionnaire = self.get_object_or_exception(self.get_queryset(), pk=org_questionnaire_id)
        event = get_object_or_404(event_models.Event, pk=event_id, organization=org_questionnaire.organization)
        org_questionnaire.events.add(event)
        return t.cast(event_models.OrganizationQuestionnaire, org_questionnaire)

    @route.delete(
        "/{org_questionnaire_id}/events/{event_id}",
        url_name="unassign_questionnaire_event",
        response={204: None},
        permissions=[QuestionnairePermission("edit_questionnaire")],
    )
    def unassign_event(self, org_questionnaire_id: UUID, event_id: UUID) -> tuple[int, None]:
        """Unassign a single event from this questionnaire (admin only).

        Removes requirement for this questionnaire from one event. Requires 'edit_questionnaire'
        permission.
        """
        org_questionnaire = self.get_object_or_exception(self.get_queryset(), pk=org_questionnaire_id)
        event = get_object_or_404(event_models.Event, pk=event_id)
        org_questionnaire.events.remove(event)
        return 204, None

    @route.put(
        "/{org_questionnaire_id}/event-series",
        url_name="replace_questionnaire_event_series",
        response=event_schema.OrganizationQuestionnaireSchema,
        permissions=[QuestionnairePermission("edit_questionnaire")],
    )
    def replace_event_series(
        self, org_questionnaire_id: UUID, payload: event_schema.EventSeriesAssignmentSchema
    ) -> event_models.OrganizationQuestionnaire:
        """Replace all assigned event series for this questionnaire (admin only).

        Batch operation to set exactly which event series require this questionnaire. Validates that
        series belong to the same organization. Requires 'edit_questionnaire' permission.
        """
        org_questionnaire = t.cast(
            event_models.OrganizationQuestionnaire,
            self.get_object_or_exception(self.get_queryset(), pk=org_questionnaire_id),
        )
        return event_questionnaire_service.replace_event_series(org_questionnaire, payload.event_series_ids)

    @route.post(
        "/{org_questionnaire_id}/event-series/{series_id}",
        url_name="assign_questionnaire_event_series",
        response=event_schema.OrganizationQuestionnaireSchema,
        permissions=[QuestionnairePermission("edit_questionnaire")],
    )
    def assign_event_series(
        self, org_questionnaire_id: UUID, series_id: UUID
    ) -> event_models.OrganizationQuestionnaire:
        """Assign a single event series to this questionnaire (admin only).

        Adds one event series that will require completion of this questionnaire. Requires
        'edit_questionnaire' permission.
        """
        org_questionnaire = self.get_object_or_exception(self.get_queryset(), pk=org_questionnaire_id)
        series = get_object_or_404(event_models.EventSeries, pk=series_id, organization=org_questionnaire.organization)
        org_questionnaire.event_series.add(series)
        return t.cast(event_models.OrganizationQuestionnaire, org_questionnaire)

    @route.delete(
        "/{org_questionnaire_id}/event-series/{series_id}",
        url_name="unassign_questionnaire_event_series",
        response={204: None},
        permissions=[QuestionnairePermission("edit_questionnaire")],
    )
    def unassign_event_series(self, org_questionnaire_id: UUID, series_id: UUID) -> tuple[int, None]:
        """Unassign a single event series from this questionnaire (admin only).

        Removes requirement for this questionnaire from one event series. Requires 'edit_questionnaire'
        permission.
        """
        org_questionnaire = self.get_object_or_exception(self.get_queryset(), pk=org_questionnaire_id)
        series = get_object_or_404(event_models.EventSeries, pk=series_id)
        org_questionnaire.event_series.remove(series)
        return 204, None
