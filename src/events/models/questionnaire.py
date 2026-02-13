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
    per_event = models.BooleanField(
        default=False,
        help_text="When True, users must complete the questionnaire separately for each event.",
    )

    objects = OrganizationQuestionnaireManager()

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["organization", "questionnaire"], name="unique_organizationquestionnaire")
        ]
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.questionnaire_id} ({self.organization_id})"


class EventQuestionnaireSubmission(TimeStampedModel):
    """Tracks questionnaire submissions per user per event.

    This model tracks all questionnaire submissions but only enforces uniqueness
    for FEEDBACK questionnaires (users can only submit feedback once per event).
    Other questionnaire types (admission, membership, generic) allow multiple
    submissions to support retakes after rejection.

    The questionnaire_type field is denormalized from OrganizationQuestionnaire
    to support the conditional unique constraint without expensive JOINs.
    """

    def __str__(self) -> str:
        return f"{self.user} - {self.event.name} - {self.questionnaire_type}"

    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name="questionnaire_submissions")
    user = models.ForeignKey(RevelUser, on_delete=models.CASCADE, related_name="event_questionnaire_submissions")
    questionnaire = models.ForeignKey(
        Questionnaire, on_delete=models.CASCADE, related_name="event_questionnaire_submissions"
    )
    submission = models.OneToOneField(
        QuestionnaireSubmission, on_delete=models.CASCADE, related_name="event_questionnaire"
    )
    questionnaire_type = models.CharField(
        choices=OrganizationQuestionnaire.QuestionnaireType.choices,
        max_length=20,
        db_index=True,
    )

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["user", "event"], name="evtqsub_user_event_idx"),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["event", "user", "questionnaire"],
                condition=models.Q(questionnaire_type=OrganizationQuestionnaire.QuestionnaireType.FEEDBACK),
                name="unique_feedback_questionnaire_per_user_event",
            )
        ]
