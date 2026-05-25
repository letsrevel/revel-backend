"""Admin classes for the event ↔ questionnaire-submission join model."""

from django.contrib import admin
from unfold.admin import ModelAdmin

from events import models
from events.admin.base import EventLinkMixin, UserLinkMixin


@admin.register(models.EventQuestionnaireSubmission)
class EventQuestionnaireSubmissionAdmin(ModelAdmin, UserLinkMixin, EventLinkMixin):  # type: ignore[misc]
    """Admin for EventQuestionnaireSubmission.

    A denormalized join tracking which questionnaire submission belongs to which
    event/user. Lifecycle is owned by the questionnaire service, so the relations
    and the denormalized ``questionnaire_type`` are read-only here.
    """

    list_display = ["__str__", "user_link", "event_link", "questionnaire", "questionnaire_type", "created_at"]
    list_filter = ["questionnaire_type", "event__organization__name"]
    list_select_related = ["user", "event", "questionnaire", "submission"]
    search_fields = ["user__username", "user__email", "event__name", "questionnaire__name"]
    autocomplete_fields = ["event", "user", "questionnaire", "submission"]
    readonly_fields = ["questionnaire_type", "created_at", "updated_at"]
    date_hierarchy = "created_at"
