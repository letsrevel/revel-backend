from django.db.models import QuerySet

from common.controllers import UserAwareController
from events import models as event_models


class QuestionnaireControllerBase(UserAwareController):
    """Shared querysets for the questionnaire sub-controllers.

    The questionnaire endpoints are split across focused mixins (core, sections,
    questions, submissions, assignments) that are composed into a single
    ``QuestionnaireController``. They all share these scoped querysets, so the
    helpers live on a common base rather than being duplicated per mixin.
    """

    def get_queryset(self) -> QuerySet[event_models.OrganizationQuestionnaire]:
        """Get the queryset based on the user."""
        return event_models.OrganizationQuestionnaire.objects.for_user(self.user())

    def get_admin_queryset(self) -> QuerySet[event_models.OrganizationQuestionnaire]:
        """Get the queryset scoped to organizations the user administers (owns or staffs)."""
        return event_models.OrganizationQuestionnaire.objects.for_admin(self.user())

    def get_organization_queryset(self) -> QuerySet[event_models.Organization]:
        """Get the queryset for the organization."""
        return event_models.Organization.objects.for_user(self.user())
