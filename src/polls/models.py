"""Poll model — wraps a Questionnaire with poll-specific concerns.

See `docs/superpowers/specs/2026-05-21-polls-design.md` for the design rationale.
"""

import typing as t
import uuid

from django.db import models

from common.models import TimeStampedModel
from events.models.mixins import ResourceVisibility
from polls.exceptions import PollAnonymityImmutableError


class Poll(TimeStampedModel):
    """A poll backed by a :class:`questionnaires.Questionnaire`.

    The Poll wraps a Questionnaire with poll-specific lifecycle, audience and
    anonymity concerns. The actual votes are stored as ``QuestionnaireSubmission``
    rows against the wrapped questionnaire — no separate vote table is needed.
    """

    class PollStatus(models.TextChoices):
        DRAFT = "draft", "Draft"
        OPEN = "open", "Open"
        CLOSED = "closed", "Closed"

    class PollResultTiming(models.TextChoices):
        AFTER_VOTE = "after_vote", "After vote"
        AFTER_CLOSE = "after_close", "After close"
        NEVER = "never", "Never"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    questionnaire = models.OneToOneField(
        "questionnaires.Questionnaire",
        on_delete=models.CASCADE,
        related_name="poll",
    )
    organization = models.ForeignKey(
        "events.Organization",
        on_delete=models.CASCADE,
        related_name="polls",
    )
    event = models.ForeignKey(
        "events.Event",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="polls",
    )

    status = models.CharField(
        choices=PollStatus.choices,
        max_length=10,
        default=PollStatus.DRAFT,
        db_index=True,
    )
    opened_at = models.DateTimeField(null=True, blank=True)
    closes_at = models.DateTimeField(null=True, blank=True, db_index=True)
    closed_at = models.DateTimeField(null=True, blank=True)
    allow_vote_changes = models.BooleanField(default=False)

    vote_visibility = models.CharField(
        choices=ResourceVisibility.choices,
        max_length=20,
        db_index=True,
    )
    result_visibility = models.CharField(
        choices=ResourceVisibility.choices,
        max_length=20,
        default=ResourceVisibility.STAFF_ONLY,
    )
    result_timing = models.CharField(
        choices=PollResultTiming.choices,
        max_length=20,
        default=PollResultTiming.NEVER,
    )

    vote_membership_tiers = models.ManyToManyField(
        "events.MembershipTier",
        blank=True,
        related_name="voteable_polls",
    )
    result_membership_tiers = models.ManyToManyField(
        "events.MembershipTier",
        blank=True,
        related_name="results_visible_polls",
    )

    staff_anonymous = models.BooleanField(default=True)
    public_anonymous = models.BooleanField(default=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=(
                    ~models.Q(
                        result_visibility__in=[
                            ResourceVisibility.PUBLIC.value,
                            ResourceVisibility.UNLISTED.value,
                        ]
                    )
                    | models.Q(public_anonymous=True)
                ),
                name="poll_public_results_must_be_anonymous",
            ),
            models.CheckConstraint(
                condition=(
                    (
                        ~models.Q(
                            vote_visibility__in=[
                                ResourceVisibility.PRIVATE.value,
                                ResourceVisibility.ATTENDEES_ONLY.value,
                            ]
                        )
                        & ~models.Q(
                            result_visibility__in=[
                                ResourceVisibility.PRIVATE.value,
                                ResourceVisibility.ATTENDEES_ONLY.value,
                            ]
                        )
                    )
                    | models.Q(event__isnull=False)
                ),
                name="poll_restricted_visibility_requires_event",
            ),
        ]
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"Poll<{self.id}> ({self.status})"

    def save(self, *args: t.Any, **kwargs: t.Any) -> None:
        """Persist the poll, enforcing anonymity immutability after creation."""
        if self.pk is not None:
            old = Poll.objects.filter(pk=self.pk).only("staff_anonymous", "public_anonymous").first()
            if old is not None and (
                old.staff_anonymous != self.staff_anonymous or old.public_anonymous != self.public_anonymous
            ):
                raise PollAnonymityImmutableError()
        super().save(*args, **kwargs)
