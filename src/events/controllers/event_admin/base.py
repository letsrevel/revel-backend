import typing as t
from uuid import UUID

from django.db.models import QuerySet

from common.controllers import UserAwareController
from events import models


class EventAdminBaseController(UserAwareController):
    """Base controller for event admin endpoints.

    Provides common methods for retrieving event querysets and instances.
    Subclasses should be decorated with @api_controller to register routes.
    """

    def get_queryset(self) -> QuerySet[models.Event]:
        """Get the queryset based on the user."""
        return models.Event.objects.for_user(self.user(), include_past=True)

    def get_one(self, event_id: UUID) -> models.Event:
        """Wrapper helper."""
        return t.cast(models.Event, self.get_object_or_exception(self.get_queryset(), pk=event_id))
