"""Announcement model for organization announcements."""

import datetime as dt
import typing as t

from django.conf import settings
from django.contrib.gis.db import models
from django.core.exceptions import ValidationError
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
        return self.filter(status=Announcement.AnnouncementStatus.DRAFT)

    def sent(self) -> t.Self:
        """Return only sent announcements."""
        return self.filter(status=Announcement.AnnouncementStatus.SENT)

    def scheduled(self) -> t.Self:
        """Return only scheduled announcements."""
        return self.filter(status=Announcement.AnnouncementStatus.SCHEDULED)


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

    def scheduled(self) -> AnnouncementQuerySet:
        """Returns only scheduled announcements."""
        return self.get_queryset().scheduled()


class Announcement(TimeStampedModel):
    """Organization announcement that can target event attendees or members."""

    class AnnouncementStatus(models.TextChoices):
        DRAFT = "draft", _("Draft")
        SCHEDULED = "scheduled", _("Scheduled")
        SENT = "sent", _("Sent")

    class ScheduleAnchor(models.TextChoices):
        EVENT_START = "event_start", _("Event start")
        EVENT_END = "event_end", _("Event end")

    class Audience(models.TextChoices):
        EVENT = "event", _("Event attendees")
        MEMBERS = "members", _("All members")
        TIERS = "tiers", _("Specific membership tiers")
        STAFF = "staff", _("Staff only")

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
        choices=AnnouncementStatus.choices,
        default=AnnouncementStatus.DRAFT,
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

    scheduled_at = models.DateTimeField(
        null=True,
        blank=True,
        db_index=True,
        help_text=_("Absolute time to send a scheduled announcement (mutually exclusive with relative scheduling)."),
    )
    schedule_anchor = models.CharField(
        max_length=20,
        choices=ScheduleAnchor.choices,
        null=True,
        blank=True,
        help_text=_("Anchor a relative schedule to the event start or end."),
    )
    schedule_offset_minutes = models.IntegerField(
        null=True,
        blank=True,
        help_text=_("Signed minutes from the anchor (-1440 = 24h before, +1440 = 24h after)."),
    )
    resend_to_new_signups = models.BooleanField(
        default=False,
        help_text=_(
            "Re-deliver to attendees who join after this announcement was first sent (event announcements only)."
        ),
    )

    objects = AnnouncementManager()

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["organization", "status"]),
            models.Index(fields=["event", "status"]),
        ]

    def __str__(self) -> str:
        return f"{self.title} ({self.organization.name})"

    @property
    def audience(self) -> "Announcement.Audience":
        """Coarse descriptor of who can see this announcement.

        Mirrors the targeting precedence used by ``get_recipients`` so the
        public card can pick an icon + label without exposing any PII. Uses the
        prefetched ``target_tiers`` cache (``len(... .all())``) to avoid an
        extra query when tiers are prefetched. Defaults to ``STAFF`` for the
        (validation-prevented) no-target case.
        """
        if self.event_id:
            return self.Audience.EVENT
        if self.target_all_members:
            return self.Audience.MEMBERS
        if len(self.target_tiers.all()):
            return self.Audience.TIERS
        return self.Audience.STAFF

    @property
    def effective_send_at(self) -> dt.datetime | None:
        """Resolve the live send time: absolute, or anchor +/- offset for relative."""
        if self.schedule_anchor is None:
            return self.scheduled_at
        if self.event is None:
            return None
        anchor_time = self.event.start if self.schedule_anchor == self.ScheduleAnchor.EVENT_START else self.event.end
        return anchor_time + dt.timedelta(minutes=self.schedule_offset_minutes or 0)

    def clean(self) -> None:
        """Validate scheduling/resend invariants (runs via TimeStampedModel.save)."""
        super().clean()
        is_relative = self.schedule_anchor is not None or self.schedule_offset_minutes is not None
        if is_relative:
            if self.schedule_anchor is None or self.schedule_offset_minutes is None:
                raise ValidationError(_("Relative scheduling requires both an anchor and an offset."))
            if self.scheduled_at is not None:
                raise ValidationError(_("Provide either an absolute time or a relative schedule, not both."))
            if self.event_id is None:
                raise ValidationError(_("Relative scheduling requires an event-targeted announcement."))
        if self.resend_to_new_signups and self.event_id is None:
            raise ValidationError(_("Re-sending to new sign-ups requires an event-targeted announcement."))
