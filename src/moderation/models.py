"""Content moderation models.

``ContentReport`` is a generic-FK flag against any model instance, mirroring
``common.models.TagAssignment``. User reports, blocklist escalations, and (future)
classifier flags all land here so there is one triage queue and one remediation path.
"""

from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from django.db import models

from accounts.models import RevelUser
from common.models import TimeStampedModel


class ContentReport(TimeStampedModel):
    """A flag that a piece of content is abusive/offensive, targeting any model via generic FK."""

    class Reason(models.TextChoices):
        OFFENSIVE = "offensive", "Offensive / hateful"
        SPAM = "spam", "Spam"
        SEXUAL = "sexual", "Sexual content"
        HARASSMENT = "harassment", "Harassment"
        OTHER = "other", "Other"

    class Status(models.TextChoices):
        OPEN = "open", "Open"
        ACTIONED = "actioned", "Actioned"
        DISMISSED = "dismissed", "Dismissed"

    class Source(models.TextChoices):
        USER_REPORT = "user_report", "User report"
        BLOCKLIST = "blocklist", "Blocklist"
        CLASSIFIER = "classifier", "Classifier"

    content_type = models.ForeignKey(ContentType, on_delete=models.CASCADE)
    object_id = models.UUIDField()
    content_object = GenericForeignKey("content_type", "object_id")
    content_snapshot = models.TextField(blank=True)

    reporter = models.ForeignKey(
        RevelUser, on_delete=models.SET_NULL, null=True, blank=True, related_name="content_reports"
    )
    source = models.CharField(max_length=20, choices=Source.choices, default=Source.USER_REPORT)
    reason = models.CharField(max_length=20, choices=Reason.choices)
    details = models.TextField(blank=True)

    status = models.CharField(max_length=20, choices=Status.choices, default=Status.OPEN, db_index=True)
    resolved_by = models.ForeignKey(
        RevelUser, on_delete=models.SET_NULL, null=True, blank=True, related_name="resolved_content_reports"
    )
    resolved_at = models.DateTimeField(null=True, blank=True)
    resolution_note = models.TextField(blank=True)
    score = models.FloatField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["content_type", "object_id"]),
            models.Index(fields=["status", "-created_at"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["content_type", "object_id", "reporter"],
                condition=models.Q(status="open"),
                name="unique_open_report_per_reporter_per_object",
            )
        ]

    def __str__(self) -> str:
        return f"{self.get_reason_display()} report ({self.status})"
