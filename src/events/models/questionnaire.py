import typing as t

from django.contrib.auth.models import AnonymousUser
from django.contrib.gis.db import models
from django.utils import timezone

from accounts.models import RevelUser
from common.models import TimeStampedModel
from questionnaires.models import Questionnaire, QuestionnaireSubmission

from .event import Event
from .event_series import EventSeries
from .organization import Organization


class OrganizationQuestionnaireQueryset(models.QuerySet["OrganizationQuestionnaire"]):
    def get_queryset(self) -> t.Self:
        """Get the base OrganizationQuestionnaire queryset."""
        # Current time
        current_time = timezone.now()

        # Annotate with absolute time difference from now, order future first (ascending), past second (descending)
        event_qs = (
            Event.objects.only("id", "name", "slug", "start")
            .annotate(
                is_future=models.Case(
                    models.When(start__gte=current_time, then=True),
                    default=False,
                    output_field=models.BooleanField(),
                ),
                time_diff=models.ExpressionWrapper(
                    models.F("start") - current_time,  # type: ignore[operator]
                    output_field=models.DurationField(),
                ),
            )
            .order_by("-is_future", "time_diff")  # True > False, so future first
        )

        # Prefetch event_series with organization to avoid N+1 queries
        event_series_qs = EventSeries.objects.select_related("organization")

        return self.select_related("organization", "questionnaire").prefetch_related(
            models.Prefetch("events", queryset=event_qs),
            models.Prefetch("event_series", queryset=event_series_qs),
        )

    def for_user(self, user: RevelUser | AnonymousUser) -> t.Self:
        """Return only OrganizationQuestionnaires whose organizations are visible to the given user."""
        visible_org_ids = Organization.objects.for_user(user).values("id")
        return self.get_queryset().filter(organization_id__in=visible_org_ids)


class OrganizationQuestionnaireManager(models.Manager["OrganizationQuestionnaire"]):
    def get_queryset(self) -> OrganizationQuestionnaireQueryset:
        """Get the base OrganizationQuestionnaire queryset."""
        return OrganizationQuestionnaireQueryset(self.model, using=self._db)

    def for_user(self, user: RevelUser | AnonymousUser) -> OrganizationQuestionnaireQueryset:
        """Return only OrganizationQuestionnaires whose organizations are visible to the given user."""
        return self.get_queryset().for_user(user)


class OrganizationQuestionnaire(TimeStampedModel):
    class QuestionnaireType(models.TextChoices):
        ADMISSION = "admission"
        MEMBERSHIP = "membership"
        FEEDBACK = "feedback"
        GENERIC = "generic"

    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="org_questionnaires")
    questionnaire = models.OneToOneField(Questionnaire, on_delete=models.CASCADE, related_name="org_questionnaires")
    event_series = models.ManyToManyField(EventSeries, related_name="org_questionnaires", blank=True)
    events = models.ManyToManyField(Event, related_name="org_questionnaires", blank=True)
    questionnaire_type = models.CharField(
        choices=QuestionnaireType.choices, default=QuestionnaireType.ADMISSION, max_length=20, db_index=True
    )
    max_submission_age = models.DurationField(
        null=True,
        blank=True,
        db_index=True,
        help_text="How long a completed submission remains valid before user must retake.",
    )
    members_exempt = models.BooleanField(default=False)

    objects = OrganizationQuestionnaireManager()

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["organization", "questionnaire"], name="unique_organizationquestionnaire")
        ]
        ordering = ["-created_at"]


class EventFeedbackSubmission(TimeStampedModel):
    """Tracks feedback questionnaire submissions per user per event.

    This model enforces the constraint that a user can only submit feedback
    once per questionnaire per event.
    """

    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name="feedback_submissions")
    user = models.ForeignKey(RevelUser, on_delete=models.CASCADE, related_name="event_feedback_submissions")
    questionnaire = models.ForeignKey(
        Questionnaire, on_delete=models.CASCADE, related_name="event_feedback_submissions"
    )
    submission = models.OneToOneField(QuestionnaireSubmission, on_delete=models.CASCADE, related_name="event_feedback")

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["event", "user", "questionnaire"],
                name="unique_feedback_per_user_event_questionnaire",
            )
        ]
