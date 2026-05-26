"""Model for bookmarking events."""

import typing as t

from django.conf import settings
from django.contrib.gis.db import models

from common.models import TimeStampedModel

if t.TYPE_CHECKING:
    from accounts.models import RevelUser

    from .event import Event


class EventBookmarkQuerySet(models.QuerySet["EventBookmark"]):
    """Custom QuerySet for EventBookmark."""

    def for_user(self, user: "RevelUser") -> t.Self:
        """Filter bookmarks for a specific user."""
        return self.filter(user=user)

    def with_event(self) -> t.Self:
        """Prefetch the bookmarked event and its organization."""
        return self.select_related("event", "event__organization")


class EventBookmarkManager(models.Manager["EventBookmark"]):
    """Manager for EventBookmark."""

    def get_queryset(self) -> EventBookmarkQuerySet:
        """Get base queryset."""
        return EventBookmarkQuerySet(self.model, using=self._db)

    def for_user(self, user: "RevelUser") -> EventBookmarkQuerySet:
        """Filter bookmarks for a specific user."""
        return self.get_queryset().for_user(user)

    def with_event(self) -> EventBookmarkQuerySet:
        """Prefetch event data."""
        return self.get_queryset().with_event()


class EventBookmark(TimeStampedModel):
    """Tracks events a user has bookmarked to find again later.

    Bookmarking is a lightweight, private "save for later" action that does not
    grant any access or change eligibility. Unbookmarking hard-deletes the row
    (no soft-archive): bookmarks carry no history worth preserving.
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="event_bookmarks",
    )
    event: models.ForeignKey["Event"] = models.ForeignKey(
        "events.Event",
        on_delete=models.CASCADE,
        related_name="bookmarks",
    )

    objects = EventBookmarkManager()

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["user", "event"],
                name="unique_user_event_bookmark",
            ),
        ]
        indexes = [
            models.Index(fields=["user", "created_at"]),
            models.Index(fields=["event"]),
        ]
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.user} bookmarked {self.event}"
