import typing as t

from django.contrib.auth.models import AnonymousUser
from django.contrib.gis.db import models
from django.db.models import Prefetch

from accounts.models import RevelUser
from common.fields import MarkdownField
from common.models import TagAssignment, TaggableMixin, TimeStampedModel

from .mixins import LogoCoverValidationMixin, SlugFromNameMixin
from .organization import Organization


class EventSeriesQuerySet(models.QuerySet["EventSeries"]):
    def with_tags(self) -> t.Self:
        """Prefetch tags and related tag objects for max performance."""
        return self.prefetch_related(
            Prefetch(
                "tags",  # the GenericRelation on Event
                queryset=TagAssignment.objects.select_related("tag"),
                to_attr="prefetched_tagassignments",  # Optional: if you want to use a custom attribute
            )
        )

    def with_organization(self) -> t.Self:
        """Get the base queryset for the eventseries."""
        return self.select_related("organization")

    def for_user(self, user: RevelUser | AnonymousUser) -> t.Self:
        """Get the queryset based on the user, using a high-performance UNION-based strategy.

        An event series is visible if its parent organization is visible.
        """
        # --- Fast paths for special users ---
        if user.is_superuser or user.is_staff:
            return self.all()
        if user.is_anonymous:
            # Simple case: only show series from public organizations.
            return self.filter(organization__visibility=Organization.Visibility.PUBLIC)

        # --- "Gather-then-filter" strategy for standard users ---

        # 1. Get the IDs of all organizations this user can see.
        # We can reuse the already optimized for_user method from the Organization model!
        visible_org_ids = Organization.objects.for_user(user).values("id")

        # 2. Filter the EventSeries queryset based on those organization IDs.
        # This results in a simple and fast query.
        return self.filter(organization_id__in=visible_org_ids).distinct()


class EventSeriesManager(models.Manager["EventSeries"]):
    def get_queryset(self) -> EventSeriesQuerySet:
        """Get the base queryset for the eventseries."""
        return EventSeriesQuerySet(self.model, using=self._db).with_tags().with_organization()

    def for_user(self, user: RevelUser | AnonymousUser) -> EventSeriesQuerySet:
        """Get the queryset based on the user."""
        return self.get_queryset().for_user(user)


class EventSeries(SlugFromNameMixin, TimeStampedModel, LogoCoverValidationMixin, TaggableMixin):
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="event_series")
    description = MarkdownField(null=True, blank=True, db_index=True)
    name = models.CharField(max_length=255, db_index=True)
    slug = models.SlugField(max_length=255, db_index=True)

    objects = EventSeriesManager()

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["organization", "name"], name="unique_event_series_name"),
            models.UniqueConstraint(fields=["organization", "slug"], name="unique_event_series_slug"),
        ]
