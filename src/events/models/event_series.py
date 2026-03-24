import typing as t

from django.contrib.auth.models import AnonymousUser
from django.contrib.gis.db import models
from django.db.models import Prefetch, Q

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
            # UNLISTED orgs are accessible like PUBLIC (e.g. via direct link);
            # discovery listings use discoverable_for_user() to hide them.
            return self.filter(organization__visibility__in=Organization.Visibility.publicly_accessible())

        # --- "Gather-then-filter" strategy for standard users ---

        # 1. Get the IDs of all organizations this user can see.
        # We can reuse the already optimized for_user method from the Organization model!
        visible_org_ids = Organization.objects.for_user(user).values("id")

        # 2. Filter the EventSeries queryset based on those organization IDs.
        # This results in a simple and fast query.
        return self.filter(organization_id__in=visible_org_ids).distinct()

    def discoverable_for_user(self, user: RevelUser | AnonymousUser) -> t.Self:
        """Get queryset for discovery listings (browse/search).

        Wraps for_user() and additionally hides event series from UNLISTED organizations
        for users who are not the owner or staff of that organization.
        """
        qs = self.for_user(user)
        if user.is_superuser or user.is_staff:
            return qs
        if user.is_anonymous:
            return qs.exclude(organization__visibility=Organization.Visibility.UNLISTED)
        is_owner_or_staff = Q(organization__owner=user) | Q(organization__staff_members=user)
        return qs.exclude(Q(organization__visibility=Organization.Visibility.UNLISTED) & ~is_owner_or_staff)


class EventSeriesManager(models.Manager["EventSeries"]):
    def get_queryset(self) -> EventSeriesQuerySet:
        """Get the base queryset for the eventseries."""
        return EventSeriesQuerySet(self.model, using=self._db)

    def for_user(self, user: RevelUser | AnonymousUser) -> EventSeriesQuerySet:
        """Get the queryset based on the user."""
        return self.get_queryset().for_user(user)

    def discoverable_for_user(self, user: RevelUser | AnonymousUser) -> EventSeriesQuerySet:
        """Get queryset for discovery listings."""
        return self.get_queryset().discoverable_for_user(user)

    def with_tags(self) -> EventSeriesQuerySet:
        """Returns a queryset prefetching tags."""
        return self.get_queryset().with_tags()

    def with_organization(self) -> EventSeriesQuerySet:
        """Returns a queryset with organization."""
        return self.get_queryset().with_organization()

    def full(self) -> EventSeriesQuerySet:
        """Returns a queryset prefetching the full event series."""
        return self.get_queryset().with_organization().with_tags()


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
        ordering = ("organization__name", "name")

    def __str__(self) -> str:
        return self.name
