# src/events/filters.py

from functools import reduce
from operator import or_
from uuid import UUID

from django.db import models
from django.db.models import Q
from django.utils import timezone
from ninja import Field, FilterSchema, Schema

from events.models import AdditionalResource, Event, Organization


class CityFilterMixin(FilterSchema):
    country: str | None = Field(None, q="city__country")  # type: ignore[call-overload]
    city_id: int | None = None


class OrganizationFilterSchema(CityFilterMixin):
    tags: list[str] | None = None

    def filter_tags(self, tags: list[str] | None) -> Q:
        """Helper to find tags only."""
        if not tags:
            return Q()
        return Q(tags__tag__name__in=tags)


class EventFilterSchema(CityFilterMixin):
    organization: UUID | None = Field(None, q="organization_id")  # type: ignore[call-overload]
    event_type: Event.Types | None = None
    visibility: Event.Visibility | None = None
    event_series: UUID | None = Field(None, q="event_series_id")  # type: ignore[call-overload]
    next_events: bool | None = True
    past_events: bool | None = None
    status: Event.Status | None = None
    tags: list[str] | None = None

    def filter_next_events(self, next_events: bool) -> Q:
        """Helper to find next events only."""
        if next_events:
            return Q(start__gte=timezone.now())
        return Q()

    def filter_past_events(self, past_events: bool) -> Q:
        """Helper to find past events only."""
        if past_events:
            return Q(start__lt=timezone.now())
        return Q()

    def filter_tags(self, tags: list[str] | None) -> Q:
        """Helper to find tags only."""
        if not tags:
            return Q()
        return (
            Q(tags__tag__name__in=tags)
            | Q(organization__tags__tag__name__in=tags)
            | Q(event_series__tags__tag__name__in=tags)
        )


class EventSeriesFilterSchema(FilterSchema):
    organization: UUID | None = Field(None, q="organization_id")  # type: ignore[call-overload]
    tags: list[str] | None = None

    def filter_tags(self, tags: list[str] | None) -> Q:
        """Helper to find tags only."""
        if not tags:
            return Q()
        return Q(tags__tag__name__in=tags) | Q(organization__tags__tag__name__in=tags)


class ResourceFilterSchema(FilterSchema):
    resource_type: AdditionalResource.ResourceTypes | None = None


class EventTokenFilterSchema(FilterSchema):
    is_active: bool | None = None
    event_id: UUID | None = None
    has_invitation: bool | None = None

    def filter_is_active(self, is_active: bool) -> Q:
        """Helper to find active tokens only."""
        if is_active:
            return Q(expires_at__gte=timezone.now())
        return Q()

    def filter_has_invitation(self, has_invitation: bool) -> Q:
        """Helper to find invitation tokens only."""
        if has_invitation:
            return Q(invitation_payload__isnull=False)
        return Q()


class OrganizationTokenFilterSchema(FilterSchema):
    is_active: bool | None = None
    organization_id: UUID | None = None
    membership_pass: bool | None = None

    def filter_is_active(self, is_active: bool) -> Q:
        """Helper to find active tokens only."""
        if is_active:
            return Q(expires_at__gte=timezone.now())
        return Q()


class DashboardOrganizationsFiltersSchema(Schema):
    owner: bool = True
    staff: bool = True
    member: bool = True
    subscriber: bool = True

    def get_organizations_queryset(self, user_id: UUID) -> models.QuerySet[Organization]:
        """This is the high-performance query builder for the organization dashboard.

        It gathers IDs from different sources using UNION to avoid expensive JOINs.
        """
        org_id_querysets = []

        if self.owner:
            owner_orgs = Organization.objects.filter(owner_id=user_id).values("id")
            org_id_querysets.append(owner_orgs)
        if self.staff:
            staff_orgs = Organization.objects.filter(staff_members__id=user_id).values("id")
            org_id_querysets.append(staff_orgs)
        if self.member:
            member_orgs = Organization.objects.filter(members__id=user_id).values("id")
            org_id_querysets.append(member_orgs)
        if self.subscriber:
            # Assuming 'subscriber' means subscribed to the organization itself
            sub_orgs = Organization.objects.filter(
                user_preferences__user_id=user_id, user_preferences__is_subscribed=True
            ).values("id")
            org_id_querysets.append(sub_orgs)

        if not org_id_querysets:
            return Organization.objects.none()

        # Combine all querysets using UNION. This is highly efficient.
        combined_ids_qs = reduce(lambda a, b: a.union(b), org_id_querysets)

        # Now, return the final, filtered queryset of full Organization objects.
        return Organization.objects.filter(id__in=combined_ids_qs)


class DashboardEventsFiltersSchema(Schema):
    owner: bool = True
    staff: bool = True
    member: bool = True
    rsvp_yes: bool = True
    rsvp_no: bool = False
    rsvp_maybe: bool = True
    got_ticket: bool = True
    got_invitation: bool = True
    subscriber: bool = True

    def get_events_queryset(self, user_id: UUID) -> models.QuerySet[Event]:  # noqa: C901
        """This is the high-performance query builder for the dashboard.

        It gathers IDs from different sources using UNION and then filters.
        This will avoid expensive JOIN queries.
        """
        # A list to hold the querysets that will be UNIONed.
        event_id_querysets = []

        # Each condition generates a simple, fast query that only fetches event IDs.
        if self.owner:
            owner_events = Event.objects.filter(organization__owner_id=user_id).values("id")
            event_id_querysets.append(owner_events)
        if self.staff:
            staff_events = Event.objects.filter(organization__staff_members__id=user_id).values("id")
            event_id_querysets.append(staff_events)
        if self.member:
            member_events = Event.objects.filter(organization__members__id=user_id).values("id")
            event_id_querysets.append(member_events)
        if self.rsvp_yes:
            rsvp_yes_events = Event.objects.filter(rsvps__user_id=user_id, rsvps__status="yes").values("id")
            event_id_querysets.append(rsvp_yes_events)
        if self.rsvp_no:
            rsvp_no_events = Event.objects.filter(rsvps__user_id=user_id, rsvps__status="no").values("id")
            event_id_querysets.append(rsvp_no_events)
        if self.rsvp_maybe:
            rsvp_maybe_events = Event.objects.filter(rsvps__user_id=user_id, rsvps__status="maybe").values("id")
            event_id_querysets.append(rsvp_maybe_events)
        if self.got_ticket:
            ticket_events = Event.objects.filter(tickets__user_id=user_id).values("id")
            event_id_querysets.append(ticket_events)
        if self.got_invitation:
            invitation_events = Event.objects.filter(invitations__user_id=user_id).values("id")
            event_id_querysets.append(invitation_events)
        if self.subscriber:
            event_sub_events = Event.objects.filter(
                user_preferences__user_id=user_id, user_preferences__is_subscribed=True
            ).values("id")
            series_sub_events = Event.objects.filter(
                event_series__user_preferences__user_id=user_id, event_series__user_preferences__is_subscribed=True
            ).values("id")
            event_id_querysets.append(event_sub_events)
            event_id_querysets.append(series_sub_events)

        if not event_id_querysets:
            return Event.objects.none()

        # Combine all querysets using UNION. This is highly efficient and removes duplicates.
        combined_ids_qs = reduce(lambda a, b: a.union(b), event_id_querysets)

        # Now, return the final, filtered queryset of full Event objects.
        return Event.objects.filter(id__in=combined_ids_qs)


class DashboardEventSeriesFiltersSchema(Schema):
    owner: bool = True
    staff: bool = True
    member: bool = True
    subscriber: bool = True

    def to_query(self, user_id: UUID) -> Q:
        """Transform the schema into a Q object."""
        filters = {
            "owner": Q(organization__owner_id=user_id),
            "staff": Q(organization__staff_members__id=user_id),
            "member": Q(organization__members__id=user_id),
            "subscriber": Q(subscriptions__user_id=user_id),
        }
        active_filters = [q_obj for field, q_obj in filters.items() if getattr(self, field)]
        return reduce(or_, active_filters) if active_filters else Q(pk__in=[])
