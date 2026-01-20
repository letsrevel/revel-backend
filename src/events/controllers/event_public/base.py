import typing as t
from uuid import UUID

from django.http import Http404
from django.utils.translation import gettext_lazy as _

from common.controllers import UserAwareController
from events import models
from events.service import event_service
from questionnaires.models import Questionnaire
from questionnaires.service import QuestionnaireService


class EventPublicBaseController(UserAwareController):
    """Base controller for public event endpoints.

    Provides common methods for retrieving event querysets and instances.
    Subclasses should be decorated with @api_controller to register routes.
    """

    def get_queryset(self, include_past: bool = False, full: bool = True) -> models.event.EventQuerySet:
        """Get the queryset based on the user."""
        allowed_ids: list[UUID] = []
        if et := self.get_event_token():
            allowed_ids = [et.event_id]
        qs = models.Event.objects.for_user(self.maybe_user(), include_past=include_past, allowed_ids=allowed_ids)
        if not full:
            return qs
        return models.Event.objects.full().for_user(
            self.maybe_user(), include_past=include_past, allowed_ids=allowed_ids
        )

    def get_one(self, event_id: UUID) -> models.Event:
        """Wrapper helper."""
        return t.cast(
            models.Event,
            self.get_object_or_exception(self.get_queryset(include_past=True).with_organization(), pk=event_id),
        )

    def get_one_by_slugs(self, org_slug: str, event_slug: str) -> models.Event:
        """Wrapper helper."""
        return t.cast(
            models.Event,
            self.get_object_or_exception(
                self.get_queryset(include_past=True).with_organization(), slug=event_slug, organization__slug=org_slug
            ),
        )

    def get_event_token(self) -> models.EventToken | None:
        """Get an event token from X-Event-Token header or et query param (legacy).

        Preferred: X-Event-Token header
        Legacy: ?et= query parameter (for backwards compatibility)
        """
        token = (
            self.context.request.META.get("HTTP_X_EVENT_TOKEN")  # type: ignore[union-attr]
            or self.context.request.GET.get("et")  # type: ignore[union-attr]
        )
        if token:
            return event_service.get_event_token(token)
        return None

    def get_questionnaire_service(self, questionnaire_id: UUID) -> QuestionnaireService:
        """Get the questionnaire for this request."""
        try:
            service = QuestionnaireService(questionnaire_id)
        except Questionnaire.DoesNotExist:
            raise Http404()
        return service

    def get_org_questionnaire_for_event(
        self, event: models.Event, questionnaire_id: UUID
    ) -> models.OrganizationQuestionnaire:
        """Validate that a questionnaire belongs to the given event.

        A questionnaire belongs to an event if there's an OrganizationQuestionnaire linking them
        via the `events` M2M, OR if the event's `event_series` is in the `event_series` M2M.

        Returns:
            The OrganizationQuestionnaire if valid.

        Raises:
            Http404: If the questionnaire doesn't belong to the event.
        """
        from django.db.models import Q

        filter_q = Q(events=event)
        if event.event_series_id:
            filter_q |= Q(event_series=event.event_series_id)

        org_questionnaire = (
            models.OrganizationQuestionnaire.objects.filter(
                questionnaire_id=questionnaire_id,
            )
            .filter(filter_q)
            .first()
        )

        if org_questionnaire is None:
            raise Http404(_("Questionnaire not found for this event."))

        return org_questionnaire
