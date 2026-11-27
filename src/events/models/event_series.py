import typing as t

from django.contrib.auth.models import AnonymousUser
from django.contrib.gis.db import models
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db.models import Exists, OuterRef, Prefetch, Q

from accounts.models import RevelUser
from common.fields import MarkdownField
from common.models import TagAssignment, TaggableMixin, TimeStampedModel
from events.utils.visibility import owner_or_staff_q

from .mixins import IMAGE_PATH_FIELDS, LogoCoverValidationMixin, SlugFromNameMixin
from .organization import Organization

# Cap the rolling generation horizon at one year to keep the daily Celery beat task bounded.
MAX_GENERATION_WINDOW_WEEKS = 52


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

    def with_is_recurring(self) -> t.Self:
        """Annotate ``is_recurring`` so consumers can distinguish recurring series from grouping-only ones.

        A series is considered recurring iff it has an attached ``RecurrenceRule`` row
        (regardless of whether occurrences have been materialized yet). The annotation uses
        an ``EXISTS`` subquery against the OneToOne reverse relation to avoid an N+1 across
        list endpoints. We import ``RecurrenceRule`` lazily to avoid an import cycle with
        ``events.models.recurrence_rule``, which itself imports from ``events.utils``.
        """
        from .recurrence_rule import RecurrenceRule

        return self.annotate(
            is_recurring=Exists(RecurrenceRule.objects.filter(event_series=OuterRef("pk"))),
        )

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

    def viewable_for_user(self, user: RevelUser | AnonymousUser) -> t.Self:
        """Series that the user can open by a direct link: the organization rule, or a visible event (#1599).

        A series has no visibility of its own. It takes the visibility of its most visible event: a public event of a
        private organization makes its series page visible. Past events count, so the page of a past event still
        links to its series.

        Use it only for the series pages and for following a series. ``for_user`` stays the gate for series passes,
        discovery listings, the dashboard and the admin controllers, so a private organization's passes stay hidden.
        """
        if user.is_superuser or user.is_staff:
            return self.all()
        from .event import Event

        via_organization = self.for_user(user).values("pk")
        via_event = (
            Event.objects.for_user(user, include_past=True).filter(event_series__isnull=False).values("event_series_id")
        )
        return self.filter(Q(pk__in=via_organization) | Q(pk__in=via_event))

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
        return qs.exclude(Q(organization__visibility=Organization.Visibility.UNLISTED) & ~owner_or_staff_q(user))


class EventSeriesManager(models.Manager["EventSeries"]):
    def get_queryset(self) -> EventSeriesQuerySet:
        """Get the base queryset for the eventseries."""
        return EventSeriesQuerySet(self.model, using=self._db)

    def for_user(self, user: RevelUser | AnonymousUser) -> EventSeriesQuerySet:
        """Get the queryset based on the user."""
        return self.get_queryset().for_user(user)

    def viewable_for_user(self, user: RevelUser | AnonymousUser) -> EventSeriesQuerySet:
        """Get the series that the user can open by a direct link (#1599)."""
        return self.get_queryset().viewable_for_user(user)

    def discoverable_for_user(self, user: RevelUser | AnonymousUser) -> EventSeriesQuerySet:
        """Get queryset for discovery listings."""
        return self.get_queryset().discoverable_for_user(user)

    def with_tags(self) -> EventSeriesQuerySet:
        """Returns a queryset prefetching tags."""
        return self.get_queryset().with_tags()

    def with_organization(self) -> EventSeriesQuerySet:
        """Returns a queryset with organization."""
        return self.get_queryset().with_organization()

    def with_is_recurring(self) -> EventSeriesQuerySet:
        """Returns a queryset annotated with ``is_recurring``."""
        return self.get_queryset().with_is_recurring()

    def full(self) -> EventSeriesQuerySet:
        """Returns a queryset prefetching the full event series."""
        return self.get_queryset().with_organization().with_tags().with_is_recurring()


class EventSeries(SlugFromNameMixin, TimeStampedModel, LogoCoverValidationMixin, TaggableMixin):
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="event_series")
    description = MarkdownField(null=True, blank=True, db_index=True)
    name = models.CharField(max_length=255, db_index=True)
    slug = models.SlugField(max_length=255, db_index=True)

    # Recurrence fields.
    # PROTECT on both template_event and recurrence_rule: deleting either side
    # individually would either silently destroy a paid-ticket-bearing series
    # (template) or leave it as a generation-broken zombie (rule). Series
    # teardown must always go through the service layer, which orchestrates
    # cleanup of the rule, the template, and all materialized occurrences in
    # the right order. PROTECT enforces this contract at the DB level so an
    # admin DELETE click can't bypass it. The fields remain nullable only
    # because they must be created after the EventSeries row (chicken-and-egg
    # at creation time in ``create_recurring_event_series``).
    template_event = models.OneToOneField(
        "Event",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="template_for_series",
    )
    recurrence_rule = models.OneToOneField(
        "events.RecurrenceRule",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="event_series",
    )
    exdates = models.JSONField(default=list, blank=True)
    is_active = models.BooleanField(default=True)
    auto_publish = models.BooleanField(default=False)
    generation_window_weeks = models.PositiveIntegerField(
        default=8,
        validators=[MinValueValidator(1), MaxValueValidator(MAX_GENERATION_WINDOW_WEEKS)],
    )
    last_generated_until = models.DateTimeField(null=True, blank=True)

    objects = EventSeriesManager()

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["organization", "name"], name="unique_event_series_name"),
            models.UniqueConstraint(fields=["organization", "slug"], name="unique_event_series_slug"),
        ]
        ordering = ("organization__name", "name")

    def __str__(self) -> str:
        return self.name

    def cascaded_image_paths(self) -> list[str]:
        """The image paths of the events that the series delete removes (template and occurrences)."""
        from .event import Event

        # Locked, so a thumbnail write on an event cannot replace a path after this read (see delete() for the order).
        rows = Event.objects.select_for_update(no_key=True).filter(event_series=self).values_list(*IMAGE_PATH_FIELDS)
        return [name for row in rows for name in row if name]

    def delete(self, *args: t.Any, **kwargs: t.Any) -> tuple[int, dict[str, int]]:
        """Delete the series and cascade-delete its events, including the template.

        ``template_event`` and ``recurrence_rule`` both use ``on_delete=PROTECT``
        to guard against direct deletion of the Event or RecurrenceRule rows
        via the admin. But deleting the series itself is the blessed teardown
        path: we must be able to reach the template event (CASCADE via
        ``Event.event_series``) without the PROTECT FK from ``this`` series
        tripping the collector. We null out both PROTECT-ing FKs in a single
        UPDATE first so the subsequent cascade can complete cleanly.

        The ``RecurrenceRule`` row is intentionally left orphaned — cleanup of
        the rule belongs in a service-level teardown helper if/when one is
        added. This is called out in the class-level comment on
        ``template_event`` and pinned by
        ``test_deleting_series_cascades_to_template_and_occurrences``.
        """
        from django.db import transaction

        with transaction.atomic():
            # Lock order: the series (NO KEY), its events, then the UPDATE below. That UPDATE changes unique columns,
            # so it takes FOR UPDATE: a duplicate that holds an event lock needs KEY SHARE on the series at commit,
            # which NO KEY allows, so the events are locked before the upgrade. Recurrence materialisation also
            # locks the series first. An organization delete locks series before events too.
            list(type(self).objects.select_for_update(no_key=True).filter(pk=self.pk).values_list("pk"))
            self.cascaded_image_paths()
            if self.pk is not None and (self.template_event_id or self.recurrence_rule_id):
                type(self).objects.filter(pk=self.pk).update(
                    template_event=None,
                    recurrence_rule=None,
                )
                self.template_event = None
                self.recurrence_rule = None
            return super().delete(*args, **kwargs)
