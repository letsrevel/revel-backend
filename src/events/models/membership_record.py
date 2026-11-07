"""Legal register of membership periods (survives account deletion)."""

import typing as t
from datetime import datetime

from django.conf import settings
from django.contrib.gis.db import models
from django.utils.translation import gettext_lazy as _

from common.models import TimeStampedModel


class MembershipRecordQuerySet(models.QuerySet["MembershipRecord"]):
    """Queries over register records."""

    def open(self) -> t.Self:
        """Records with no end date."""
        return self.filter(valid_until__isnull=True)

    def active_on(self, when: datetime) -> t.Self:
        """Non-revoked records covering ``when`` (``valid_from <= when < valid_until``)."""
        return (
            self.exclude(ended_reason=MembershipRecord.EndedReason.REVOKED)
            .filter(valid_from__lte=when)
            .filter(models.Q(valid_until__isnull=True) | models.Q(valid_until__gt=when))
        )


class MembershipRecord(TimeStampedModel):
    """One membership period of one person in one organization.

    A person who joins, leaves and rejoins has several rows. Identity and acceptance evidence are
    snapshotted so the record survives deletion of the account, tier or questionnaire submission.
    """

    class Kind(models.TextChoices):
        STANDARD = "standard", _("Standard")
        TEMPORARY = "temporary", _("Temporary")

    class EndedReason(models.TextChoices):
        CANCELLED = "cancelled", _("Cancelled")
        EXPELLED = "expelled", _("Expelled")
        REMOVED = "removed", _("Removed")
        REVOKED = "revoked", _("Revoked")
        TIER_CHANGED = "tier_changed", _("Tier changed")

    class Source(models.TextChoices):
        APPLICATION = "application", _("Application")
        STAFF = "staff", _("Staff")
        TOKEN = "token", _("Token")
        SUBSCRIPTION = "subscription", _("Subscription")
        TICKET = "ticket", _("Ticket")
        MANUAL = "manual", _("Manual")
        IMPORT = "import", _("Import")
        BACKFILL = "backfill", _("Backfill")

    organization = models.ForeignKey("events.Organization", on_delete=models.CASCADE, related_name="membership_records")
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="membership_records",
    )
    full_name = models.CharField(max_length=255)
    # Snapshot, not input: deliberately not an EmailField so full_clean on save never rejects a stored address.
    email = models.CharField(max_length=254, blank=True)
    kind = models.CharField(max_length=20, choices=Kind.choices, default=Kind.STANDARD)
    tier = models.ForeignKey(
        "events.MembershipTier", on_delete=models.SET_NULL, null=True, blank=True, related_name="records"
    )
    tier_name = models.CharField(max_length=255, blank=True)
    valid_from = models.DateTimeField()
    valid_until = models.DateTimeField(null=True, blank=True)
    ended_reason = models.CharField(max_length=20, choices=EndedReason.choices, null=True, blank=True)
    source = models.CharField(max_length=20, choices=Source.choices)
    ticket = models.ForeignKey("events.Ticket", on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    event = models.ForeignKey("events.Event", on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    subscription = models.ForeignKey(
        "events.MembershipSubscription", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    questionnaire_submission = models.ForeignKey(
        "questionnaires.QuestionnaireSubmission", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    accepted_at = models.DateTimeField(null=True, blank=True)
    questionnaire_name = models.CharField(max_length=255, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_membership_records",
    )

    objects = MembershipRecordQuerySet.as_manager()

    class Meta:
        indexes = [
            models.Index(fields=["organization", "valid_from"]),
            models.Index(fields=["organization", "user"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "user"],
                # Literal "standard": keep in sync with Kind.STANDARD (a nested Meta cannot see Kind).
                condition=models.Q(valid_until__isnull=True, kind="standard", user__isnull=False),
                name="unique_open_standard_record_per_user",
            ),
            models.CheckConstraint(
                condition=models.Q(valid_until__isnull=True) | models.Q(valid_until__gte=models.F("valid_from")),
                name="membership_record_valid_range",
            ),
            models.UniqueConstraint(
                fields=["ticket"],
                condition=models.Q(ticket__isnull=False),
                name="unique_membership_record_ticket",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.full_name} @ {self.organization_id} ({self.valid_from:%Y-%m-%d})"
