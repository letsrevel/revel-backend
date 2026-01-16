"""Models for following organizations and event series."""

import typing as t

from django.conf import settings
from django.contrib.gis.db import models

from common.models import TimeStampedModel

if t.TYPE_CHECKING:
    from accounts.models import RevelUser

    from .event_series import EventSeries
    from .organization import Organization


class OrganizationFollowQuerySet(models.QuerySet["OrganizationFollow"]):
    """Custom QuerySet for OrganizationFollow."""

    def active(self) -> t.Self:
        """Return only active (non-archived) follows."""
        return self.filter(is_archived=False)

    def for_user(self, user: "RevelUser") -> t.Self:
        """Filter follows for a specific user."""
        return self.filter(user=user)

    def with_organization(self) -> t.Self:
        """Prefetch organization data."""
        return self.select_related("organization")


class OrganizationFollowManager(models.Manager["OrganizationFollow"]):
    """Manager for OrganizationFollow."""

    def get_queryset(self) -> OrganizationFollowQuerySet:
        """Get base queryset."""
        return OrganizationFollowQuerySet(self.model, using=self._db)

    def active(self) -> OrganizationFollowQuerySet:
        """Return only active follows."""
        return self.get_queryset().active()

    def for_user(self, user: "RevelUser") -> OrganizationFollowQuerySet:
        """Filter follows for a specific user."""
        return self.get_queryset().for_user(user)

    def with_organization(self) -> OrganizationFollowQuerySet:
        """Prefetch organization data."""
        return self.get_queryset().with_organization()


class OrganizationFollow(TimeStampedModel):
    """Tracks users following organizations.

    Users can follow public organizations to receive notifications about
    new events, announcements, and updates.
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="organization_follows",
    )
    organization: models.ForeignKey["Organization"] = models.ForeignKey(
        "events.Organization",
        on_delete=models.CASCADE,
        related_name="followers",
    )

    # Notification preferences for this follow
    notify_new_events = models.BooleanField(
        default=True,
        help_text="Receive notifications when the organization creates new events",
    )
    notify_announcements = models.BooleanField(
        default=True,
        help_text="Receive notifications when the organization makes announcements",
    )

    # Visibility flag (reserved for future use - defaults to private)
    is_public = models.BooleanField(
        default=False,
        help_text="Whether this follow is publicly visible on the user's profile",
    )

    # Soft archive flag for unfollowing without losing history
    is_archived = models.BooleanField(
        default=False,
        db_index=True,
        help_text="Archived follows are hidden but preserved for history",
    )

    objects = OrganizationFollowManager()

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["user", "organization"],
                name="unique_user_organization_follow",
            ),
        ]
        indexes = [
            models.Index(fields=["organization", "is_archived"]),
            models.Index(fields=["user", "is_archived"]),
        ]
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.user} follows {self.organization}"


class EventSeriesFollowQuerySet(models.QuerySet["EventSeriesFollow"]):
    """Custom QuerySet for EventSeriesFollow."""

    def active(self) -> t.Self:
        """Return only active (non-archived) follows."""
        return self.filter(is_archived=False)

    def for_user(self, user: "RevelUser") -> t.Self:
        """Filter follows for a specific user."""
        return self.filter(user=user)

    def with_event_series(self) -> t.Self:
        """Prefetch event series and organization data."""
        return self.select_related("event_series", "event_series__organization")


class EventSeriesFollowManager(models.Manager["EventSeriesFollow"]):
    """Manager for EventSeriesFollow."""

    def get_queryset(self) -> EventSeriesFollowQuerySet:
        """Get base queryset."""
        return EventSeriesFollowQuerySet(self.model, using=self._db)

    def active(self) -> EventSeriesFollowQuerySet:
        """Return only active follows."""
        return self.get_queryset().active()

    def for_user(self, user: "RevelUser") -> EventSeriesFollowQuerySet:
        """Filter follows for a specific user."""
        return self.get_queryset().for_user(user)

    def with_event_series(self) -> EventSeriesFollowQuerySet:
        """Prefetch event series data."""
        return self.get_queryset().with_event_series()


class EventSeriesFollow(TimeStampedModel):
    """Tracks users following event series.

    Users can follow event series to receive notifications when new events
    are added to the series or when the series is updated.
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="event_series_follows",
    )
    event_series: models.ForeignKey["EventSeries"] = models.ForeignKey(
        "events.EventSeries",
        on_delete=models.CASCADE,
        related_name="followers",
    )

    # Notification preferences for this follow
    notify_new_events = models.BooleanField(
        default=True,
        help_text="Receive notifications when new events are added to the series",
    )

    # Visibility flag (reserved for future use - defaults to private)
    is_public = models.BooleanField(
        default=False,
        help_text="Whether this follow is publicly visible on the user's profile",
    )

    # Soft archive flag for unfollowing without losing history
    is_archived = models.BooleanField(
        default=False,
        db_index=True,
        help_text="Archived follows are hidden but preserved for history",
    )

    objects = EventSeriesFollowManager()

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["user", "event_series"],
                name="unique_user_event_series_follow",
            ),
        ]
        indexes = [
            models.Index(fields=["event_series", "is_archived"]),
            models.Index(fields=["user", "is_archived"]),
        ]
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.user} follows {self.event_series}"
