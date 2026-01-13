from django.db.models import QuerySet

from common.controllers import UserAwareController
from events import models


class OrganizationAdminBaseController(UserAwareController):
    """Base controller for organization admin endpoints.

    Provides common methods for retrieving organization querysets and instances.
    Subclasses should be decorated with @api_controller to register routes.
    """

    def get_queryset(self) -> QuerySet[models.Organization]:
        """Get the queryset based on the user."""
        return models.Organization.objects.for_user(self.user())

    def get_one(self, slug: str) -> models.Organization:
        """Get one organization."""
        return self.get_object_or_exception(self.get_queryset(), slug=slug)  # type: ignore[no-any-return]
