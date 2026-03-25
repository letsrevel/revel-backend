import typing as t
from uuid import UUID

from django.http import Http404
from django.utils.translation import gettext_lazy as _
from ninja.errors import HttpError
from ninja_extra.exceptions import NotFound

from common.controllers import UserAwareController
from events import models
from events.service import event_service
from events.service.tokens import TokenRejection
from questionnaires.models import Questionnaire
from questionnaires.service import QuestionnaireService

_TOKEN_GONE_MESSAGES = {
    "expired": _("This invitation link has expired."),
    "used_up": _("This invitation link has reached its maximum number of uses."),
}


class EventPublicBaseController(UserAwareController):
    """Base controller for public event endpoints.

    Provides common methods for retrieving event querysets and instances.
    Subclasses should be decorated with @api_controller to register routes.
    """

    _token_rejection: TokenRejection | None = None

    def get_queryset(self, include_past: bool = False, full: bool = True) -> models.event.EventQuerySet:
        """Get the queryset based on the user."""
        allowed_ids: list[UUID] = []
        if et := self.get_event_token():
            allowed_ids = [et.event_id]
        base = models.Event.objects.full() if full else models.Event.objects
        return base.for_user(self.maybe_user(), include_past=include_past, allowed_ids=allowed_ids)

    def get_discovery_queryset(self, include_past: bool = False) -> models.event.EventQuerySet:
        """Get the queryset for discovery listings (hides UNLISTED from non-owner/non-staff users)."""
        return models.Event.objects.full().discoverable_for_user(self.maybe_user(), include_past=include_past)

    def _raise_if_token_gone(self, event_id: UUID | None = None) -> None:
        """Raise 410 if the request carried a token that was rejected.

        Args:
            event_id: When provided, only raise 410 if the rejected token
                belongs to this event (prevents info-leakage for unrelated events).
        """
        if self._token_rejection is None:
            return
        if event_id is not None and event_id != self._token_rejection.event_id:
            return
        raise HttpError(410, str(_TOKEN_GONE_MESSAGES[self._token_rejection.reason]))

    def get_one(self, event_id: UUID) -> models.Event:
        """Wrapper helper."""
        qs = self.get_queryset(include_past=True).with_organization()
        try:
            return t.cast(models.Event, self.get_object_or_exception(qs, pk=event_id))
        except NotFound:
            self._raise_if_token_gone(event_id=event_id)
            raise

    def get_one_by_slugs(self, org_slug: str, event_slug: str) -> models.Event:
        """Wrapper helper."""
        qs = self.get_queryset(include_past=True).with_organization()
        try:
            return t.cast(
                models.Event,
                self.get_object_or_exception(qs, slug=event_slug, organization__slug=org_slug),
            )
        except NotFound:
            if self._token_rejection is not None:
                event_id = (
                    models.Event.objects.filter(slug=event_slug, organization__slug=org_slug)
                    .values_list("id", flat=True)
                    .first()
                )
                if event_id is not None:
                    self._raise_if_token_gone(event_id=event_id)
            raise

    def get_event_token(self) -> models.EventToken | None:
        """Get an event token from X-Event-Token header or et query param (legacy).

        Preferred: X-Event-Token header
        Legacy: ?et= query parameter (for backwards compatibility)

        Side effect: if the token exists but is expired/used up, stores
        the rejection reason in ``self._token_rejection`` so that downstream
        helpers can raise 410 instead of 404.
        """
        token = (
            self.context.request.META.get("HTTP_X_EVENT_TOKEN")  # type: ignore[union-attr]
            or self.context.request.GET.get("et")  # type: ignore[union-attr]
        )
        if not token:
            return None
        event_token = event_service.get_event_token(token)
        if event_token is None:
            self._token_rejection = event_service.get_token_rejection_reason(token)
        return event_token

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
