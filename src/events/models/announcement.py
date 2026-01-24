"""Announcement model for organization announcements."""

import typing as t

from django.conf import settings
from django.contrib.gis.db import models
from django.utils.translation import gettext_lazy as _

from common.fields import MarkdownField
from common.models import TimeStampedModel


class AnnouncementQuerySet(models.QuerySet["Announcement"]):
    """Custom queryset for Announcement model."""

    def with_organization(self) -> t.Self:
        """Select the related organization."""
        return self.select_related("organization")

    def with_event(self) -> t.Self:
        """Select the related event."""
        return self.select_related("event")

    def with_created_by(self) -> t.Self:
        """Select the related created_by user."""
        return self.select_related("created_by")

    def with_target_tiers(self) -> t.Self:
        """Prefetch target tiers."""
        return self.prefetch_related("target_tiers")

    def full(self) -> t.Self:
        """Select all related fields for complete announcement data."""
        return self.with_organization().with_event().with_created_by().with_target_tiers()

    def drafts(self) -> t.Self:
        """Return only draft announcements."""
        from events.models.announcement import Announcement

        return self.filter(status=Announcement.Status.DRAFT)

    def sent(self) -> t.Self:
        """Return only sent announcements."""
        from events.models.announcement import Announcement

        return self.filter(status=Announcement.Status.SENT)


class AnnouncementManager(models.Manager["Announcement"]):
    """Custom manager for Announcement."""

    def get_queryset(self) -> AnnouncementQuerySet:
        """Get base queryset."""
        return AnnouncementQuerySet(self.model, using=self._db)

    def with_organization(self) -> AnnouncementQuerySet:
        """Returns a queryset with the organization selected."""
        return self.get_queryset().with_organization()

    def with_event(self) -> AnnouncementQuerySet:
        """Returns a queryset with the event selected."""
        return self.get_queryset().with_event()

    def with_created_by(self) -> AnnouncementQuerySet:
        """Returns a queryset with the created_by user selected."""
        return self.get_queryset().with_created_by()

    def with_target_tiers(self) -> AnnouncementQuerySet:
        """Returns a queryset with target tiers prefetched."""
        return self.get_queryset().with_target_tiers()

    def full(self) -> AnnouncementQuerySet:
        """Returns a queryset with all related fields selected."""
        return self.get_queryset().full()

    def drafts(self) -> AnnouncementQuerySet:
        """Returns only draft announcements."""
        return self.get_queryset().drafts()

    def sent(self) -> AnnouncementQuerySet:
        """Returns only sent announcements."""
        return self.get_queryset().sent()


class Announcement(TimeStampedModel):
    """Organization announcement that can target event attendees or members."""

    class Status(models.TextChoices):
        DRAFT = "draft", _("Draft")
        SENT = "sent", _("Sent")

    organization = models.ForeignKey(
        "events.Organization",
        on_delete=models.CASCADE,
        related_name="announcements",
    )
    event = models.ForeignKey(
        "events.Event",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="announcements",
    )

    # Targeting options (mutually exclusive at schema level)
    target_all_members = models.BooleanField(
        default=False,
        help_text=_("Target all active organization members"),
    )
    target_tiers = models.ManyToManyField(
        "events.MembershipTier",
        blank=True,
        related_name="targeted_announcements",
        help_text=_("Target members of specific membership tiers"),
    )
    target_staff_only = models.BooleanField(
        default=False,
        help_text=_("Target only organization staff members"),
    )

    title = models.CharField(max_length=150)
    body = MarkdownField()

    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.DRAFT,
        db_index=True,
    )
    past_visibility = models.BooleanField(
        default=True,
        help_text=_("Allow new attendees/members to see this announcement after it was sent"),
    )

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name="created_announcements",
    )
    sent_at = models.DateTimeField(null=True, blank=True, db_index=True)
    recipient_count = models.PositiveIntegerField(default=0)

    objects = AnnouncementManager()

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["organization", "status"]),
            models.Index(fields=["event", "status"]),
        ]

    def __str__(self) -> str:
        return f"{self.title} ({self.organization.name})"
