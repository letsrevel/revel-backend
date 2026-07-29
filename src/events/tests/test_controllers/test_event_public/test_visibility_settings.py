"""Wire contract for granular event visibility settings (#792).

Every count/capacity surface listed in the issue is exercised across the four
viewer classes (anonymous, unrelated authenticated, attendee, staff/owner) with
the toggles both on and off.
"""

import typing as t

import pytest
from django.test.client import Client
from django.urls import reverse

from accounts.models import RevelUser
from events.models import (
    AttendeeVisibilityFlag,
    Event,
    EventRSVP,
    Organization,
    OrganizationStaff,
    TicketTier,
)

pytestmark = pytest.mark.django_db


@pytest.fixture
def anonymous_client() -> Client:
    """An unauthenticated API client."""
    return Client()


@pytest.fixture
def attendee_client(public_event: Event, django_user_model: type[RevelUser]) -> Client:
    """A client for a user attending ``public_event`` with a confirmed RSVP."""
    from ninja_jwt.tokens import RefreshToken

    user = django_user_model.objects.create_user(
        username="attendee792@example.com", email="attendee792@example.com", password="pass"
    )
    EventRSVP.objects.create(user=user, event=public_event, status=EventRSVP.RsvpStatus.YES)
    refresh = RefreshToken.for_user(user)
    return Client(HTTP_AUTHORIZATION=f"Bearer {refresh.access_token!s}")  # type: ignore[attr-defined]


@pytest.fixture
def counted_event(public_event: Event) -> Event:
    """``public_event`` with a non-zero attendee count (capacity is 10)."""
    public_event.attendee_count = 4
    public_event.save(update_fields=["attendee_count"])
    return public_event


def _set_visibility(event: Event, **settings: bool) -> None:
    """Persist a partial visibility-settings blob on ``event``."""
    event.visibility_settings = settings
    event.save(update_fields=["visibility_settings"])


def _detail(client: Client, event: Event) -> dict[str, t.Any]:
    """GET the event detail endpoint and return the decoded body."""
    response = client.get(reverse("api:get_event", kwargs={"event_id": str(event.id)}))
    assert response.status_code == 200, response.content
    return t.cast(dict[str, t.Any], response.json())


class TestDetailCounts:
    """``attendee_count`` on the detail endpoint."""

    def test_visible_by_default_to_anonymous(self, anonymous_client: Client, counted_event: Event) -> None:
        assert _detail(anonymous_client, counted_event)["attendee_count"] == 4

    def test_hidden_from_anonymous(self, anonymous_client: Client, counted_event: Event) -> None:
        _set_visibility(counted_event, show_attendee_count=False)

        assert _detail(anonymous_client, counted_event)["attendee_count"] is None

    def test_hidden_from_authenticated_nonmember(self, nonmember_client: Client, counted_event: Event) -> None:
        _set_visibility(counted_event, show_attendee_count=False)

        assert _detail(nonmember_client, counted_event)["attendee_count"] is None

    def test_hidden_from_attendee(self, attendee_client: Client, counted_event: Event) -> None:
        """Attending does not buy you the head count."""
        _set_visibility(counted_event, show_attendee_count=False)

        assert _detail(attendee_client, counted_event)["attendee_count"] is None

    def test_visible_to_owner(self, organization_owner_client: Client, counted_event: Event) -> None:
        _set_visibility(counted_event, show_attendee_count=False)

        assert _detail(organization_owner_client, counted_event)["attendee_count"] == 4

    def test_visible_to_org_staff(
        self,
        organization_staff_client: Client,
        counted_event: Event,
        staff_member: OrganizationStaff,
    ) -> None:
        _set_visibility(counted_event, show_attendee_count=False)

        assert _detail(organization_staff_client, counted_event)["attendee_count"] == 4

    def test_capacity_toggle_does_not_hide_the_count(self, anonymous_client: Client, counted_event: Event) -> None:
        _set_visibility(counted_event, show_capacity=False)

        assert _detail(anonymous_client, counted_event)["attendee_count"] == 4


class TestDetailCapacity:
    """``max_attendees`` and ``seats_held`` on the detail endpoint."""

    def test_visible_by_default(self, anonymous_client: Client, counted_event: Event) -> None:
        body = _detail(anonymous_client, counted_event)

        assert body["max_attendees"] == 10
        assert body["seats_held"] == 0

    def test_hidden_from_anonymous(self, anonymous_client: Client, counted_event: Event) -> None:
        _set_visibility(counted_event, show_capacity=False)

        body = _detail(anonymous_client, counted_event)

        assert body["max_attendees"] is None
        assert body["seats_held"] is None

    def test_hidden_from_attendee(self, attendee_client: Client, counted_event: Event) -> None:
        _set_visibility(counted_event, show_capacity=False)

        assert _detail(attendee_client, counted_event)["max_attendees"] is None

    def test_visible_to_owner(self, organization_owner_client: Client, counted_event: Event) -> None:
        _set_visibility(counted_event, show_capacity=False)

        body = _detail(organization_owner_client, counted_event)

        assert body["max_attendees"] == 10
        assert body["seats_held"] == 0

    def test_count_toggle_does_not_hide_capacity(self, anonymous_client: Client, counted_event: Event) -> None:
        _set_visibility(counted_event, show_attendee_count=False)

        assert _detail(anonymous_client, counted_event)["max_attendees"] == 10


class TestIsFullWire:
    """``is_full`` is served to everyone, hidden counts or not."""

    def test_false_when_not_full(self, anonymous_client: Client, counted_event: Event) -> None:
        _set_visibility(counted_event, show_attendee_count=False, show_capacity=False)

        assert _detail(anonymous_client, counted_event)["is_full"] is False

    def test_true_when_full_and_counts_hidden(self, anonymous_client: Client, public_event: Event) -> None:
        public_event.attendee_count = 10  # == max_attendees
        public_event.visibility_settings = {"show_attendee_count": False, "show_capacity": False}
        public_event.save(update_fields=["attendee_count", "visibility_settings"])

        body = _detail(anonymous_client, public_event)

        assert body["is_full"] is True
        assert body["attendee_count"] is None
        assert body["max_attendees"] is None


class TestVisibilitySettingsExposure:
    """The toggles themselves are part of the public contract."""

    def test_defaults_are_serialized(self, anonymous_client: Client, public_event: Event) -> None:
        assert _detail(anonymous_client, public_event)["visibility_settings"] == {
            "show_attendee_count": True,
            "show_capacity": True,
            "show_attendee_list": True,
            "show_pronoun_distribution": False,
            "address_visibility": "public",
        }

    def test_partial_blob_is_filled_in(self, anonymous_client: Client, public_event: Event) -> None:
        """Clients always receive every key, so they can branch on it."""
        _set_visibility(public_event, show_capacity=False)

        assert _detail(anonymous_client, public_event)["visibility_settings"] == {
            "show_attendee_count": True,
            "show_capacity": False,
            "show_attendee_list": True,
            "show_pronoun_distribution": False,
            "address_visibility": "public",
        }


class TestListAndCalendar:
    """The list and calendar endpoints serve the same gated fields."""

    def test_list_hides_counts(self, anonymous_client: Client, counted_event: Event) -> None:
        _set_visibility(counted_event, show_attendee_count=False, show_capacity=False)

        response = anonymous_client.get(reverse("api:list_events"))

        assert response.status_code == 200
        rows = [row for row in response.json()["results"] if row["id"] == str(counted_event.id)]
        assert rows, "event missing from the listing"
        assert rows[0]["attendee_count"] is None
        assert rows[0]["max_attendees"] is None
        assert rows[0]["seats_held"] is None

    def test_calendar_hides_counts(self, anonymous_client: Client, counted_event: Event) -> None:
        _set_visibility(counted_event, show_attendee_count=False)

        response = anonymous_client.get(
            reverse("api:calendar_events"),
            {"year": counted_event.start.year, "month": counted_event.start.month},
        )

        assert response.status_code == 200
        rows = [row for row in response.json() if row["id"] == str(counted_event.id)]
        assert rows, "event missing from the calendar"
        assert rows[0]["attendee_count"] is None


class TestTicketTierTotalAvailable:
    """``total_available`` on ``GET /events/{id}/tickets/tiers``."""

    @pytest.fixture
    def limited_tier(self, public_event: Event) -> TicketTier:
        """A publicly purchasable tier with finite stock."""
        return TicketTier.objects.create(
            event=public_event,
            name="Limited",
            total_quantity=25,
            quantity_sold=5,
            payment_method=TicketTier.PaymentMethod.FREE,
        )

    def _tier_row(self, client: Client, event: Event, tier: TicketTier) -> dict[str, t.Any]:
        response = client.get(reverse("api:tier_list", kwargs={"event_id": str(event.id)}))
        assert response.status_code == 200, response.content
        rows = [row for row in response.json() if row["id"] == str(tier.id)]
        assert rows, "tier missing from the listing"
        return t.cast(dict[str, t.Any], rows[0])

    def test_visible_by_default(self, anonymous_client: Client, public_event: Event, limited_tier: TicketTier) -> None:
        assert self._tier_row(anonymous_client, public_event, limited_tier)["total_available"] == 20

    def test_hidden_from_anonymous(
        self, anonymous_client: Client, public_event: Event, limited_tier: TicketTier
    ) -> None:
        _set_visibility(public_event, show_capacity=False)

        assert self._tier_row(anonymous_client, public_event, limited_tier)["total_available"] is None

    def test_hidden_from_authenticated_nonmember(
        self, nonmember_client: Client, public_event: Event, limited_tier: TicketTier
    ) -> None:
        _set_visibility(public_event, show_capacity=False)

        assert self._tier_row(nonmember_client, public_event, limited_tier)["total_available"] is None

    def test_visible_to_owner(
        self, organization_owner_client: Client, public_event: Event, limited_tier: TicketTier
    ) -> None:
        _set_visibility(public_event, show_capacity=False)

        assert self._tier_row(organization_owner_client, public_event, limited_tier)["total_available"] == 20

    def test_already_built_schema_is_passed_through(self, limited_tier: TicketTier) -> None:
        """Ninja re-validates schemas a service returned; those carry no ``event``.

        Guards the guest-checkout path (``BatchCheckoutResponse``), where the
        resolver is handed an assembled ``TicketTierSchema`` on the second pass.
        """
        from events.schema.ticket_tier import TicketTierSchema

        built = TicketTierSchema.from_orm(limited_tier)

        assert TicketTierSchema.resolve_total_available(built, None) == 20


class TestPronounDistribution:
    """The second exact head count behind ``public_pronoun_distribution``."""

    @pytest.fixture
    def pronoun_event(self, public_event: Event, django_user_model: type[RevelUser]) -> Event:
        """A public-distribution event with one attendee who declared pronouns."""
        _set_visibility(public_event, show_pronoun_distribution=True)
        user = django_user_model.objects.create_user(
            username="pronouns792@example.com",
            email="pronouns792@example.com",
            password="pass",
            pronouns="they/them",
        )
        EventRSVP.objects.create(user=user, event=public_event, status=EventRSVP.RsvpStatus.YES)
        return public_event

    def _get(self, client: Client, event: Event) -> dict[str, t.Any]:
        response = client.get(reverse("api:event_pronoun_distribution", kwargs={"event_id": str(event.id)}))
        assert response.status_code == 200, response.content
        return t.cast(dict[str, t.Any], response.json())

    def test_visible_by_default(self, nonmember_client: Client, pronoun_event: Event) -> None:
        body = self._get(nonmember_client, pronoun_event)

        assert body["total_attendees"] == 1
        assert body["distribution"] == [{"pronouns": "they/them", "count": 1}]

    def test_totals_and_distribution_hidden_when_counts_hidden(
        self, nonmember_client: Client, pronoun_event: Event
    ) -> None:
        """The per-pronoun counts sum back to the total, so both are redacted."""
        _set_visibility(pronoun_event, show_attendee_count=False)

        body = self._get(nonmember_client, pronoun_event)

        assert body["total_attendees"] is None
        assert body["total_with_pronouns"] is None
        assert body["total_without_pronouns"] is None
        assert body["distribution"] == []

    def test_owner_still_sees_totals(self, organization_owner_client: Client, pronoun_event: Event) -> None:
        _set_visibility(pronoun_event, show_attendee_count=False)

        body = self._get(organization_owner_client, pronoun_event)

        assert body["total_attendees"] == 1
        assert body["distribution"] == [{"pronouns": "they/them", "count": 1}]


class TestDietarySummary:
    """Per-restriction aggregate counts on the authenticated dietary endpoint."""

    @pytest.fixture
    def dietary_event(self, public_event: Event, django_user_model: type[RevelUser]) -> Event:
        """An event with one attendee declaring a public dietary restriction."""
        from accounts.models import DietaryRestriction, FoodItem

        user = django_user_model.objects.create_user(
            username="diet792@example.com", email="diet792@example.com", password="pass"
        )
        EventRSVP.objects.create(user=user, event=public_event, status=EventRSVP.RsvpStatus.YES)
        food_item, _ = FoodItem.objects.get_or_create(name="peanut")
        DietaryRestriction.objects.create(
            user=user,
            food_item=food_item,
            restriction_type=DietaryRestriction.RestrictionType.ALLERGY,
            is_public=True,
        )
        return public_event

    def _get(self, client: Client, event: Event) -> dict[str, t.Any]:
        response = client.get(reverse("api:event_dietary_summary", kwargs={"event_id": str(event.id)}))
        assert response.status_code == 200, response.content
        return t.cast(dict[str, t.Any], response.json())

    def test_counts_visible_by_default(self, nonmember_client: Client, dietary_event: Event) -> None:
        body = self._get(nonmember_client, dietary_event)

        assert body["restrictions"][0]["attendee_count"] == 1

    def test_counts_hidden_for_non_staff(self, nonmember_client: Client, dietary_event: Event) -> None:
        """The entries survive (you still need to know what to cook) — the numbers don't."""
        _set_visibility(dietary_event, show_attendee_count=False)

        body = self._get(nonmember_client, dietary_event)

        assert body["restrictions"][0]["food_item"] == "peanut"
        assert body["restrictions"][0]["attendee_count"] is None

    def test_owner_still_sees_counts(self, organization_owner_client: Client, dietary_event: Event) -> None:
        _set_visibility(dietary_event, show_attendee_count=False)

        body = self._get(organization_owner_client, dietary_event)

        assert body["restrictions"][0]["attendee_count"] == 1


class TestAttendeeListEndpoint:
    """``show_attendee_list`` ANDs with the per-user visibility matrix on the wire."""

    @pytest.fixture
    def visible_attendee(self, public_event: Event, nonmember_user: RevelUser, member_user: RevelUser) -> RevelUser:
        """``member_user`` attends and has opted into being visible to ``nonmember_user``."""
        EventRSVP.objects.create(user=member_user, event=public_event, status=EventRSVP.RsvpStatus.YES)
        AttendeeVisibilityFlag.objects.create(
            user=nonmember_user, event=public_event, target=member_user, is_visible=True
        )
        return member_user

    def _get(self, client: Client, event: Event) -> dict[str, t.Any]:
        response = client.get(reverse("api:event_attendee_list", kwargs={"event_id": str(event.id)}))
        assert response.status_code == 200, response.content
        return t.cast(dict[str, t.Any], response.json())

    def test_opted_in_attendee_is_listed_by_default(
        self, nonmember_client: Client, public_event: Event, visible_attendee: RevelUser
    ) -> None:
        assert self._get(nonmember_client, public_event)["count"] == 1

    def test_event_toggle_hides_opted_in_attendee(
        self, nonmember_client: Client, public_event: Event, visible_attendee: RevelUser
    ) -> None:
        _set_visibility(public_event, show_attendee_list=False)

        assert self._get(nonmember_client, public_event)["count"] == 0

    def test_owner_still_sees_the_list(
        self, organization_owner_client: Client, public_event: Event, visible_attendee: RevelUser
    ) -> None:
        _set_visibility(public_event, show_attendee_list=False)

        assert self._get(organization_owner_client, public_event)["count"] == 1

    def test_other_toggles_do_not_hide_the_list(
        self, nonmember_client: Client, public_event: Event, visible_attendee: RevelUser
    ) -> None:
        _set_visibility(public_event, show_attendee_count=False, show_capacity=False)

        assert self._get(nonmember_client, public_event)["count"] == 1


class TestOrganizerRoundTrip:
    """The organizer-facing create/edit endpoints round-trip the field."""

    def test_create_persists_the_settings(self, organization_owner_client: Client, organization: Organization) -> None:
        response = organization_owner_client.post(
            reverse("api:create_event", kwargs={"slug": organization.slug}),
            data={
                "name": "Discreet Event",
                "start": "2099-01-01T18:00:00Z",
                "event_type": Event.EventType.PUBLIC.value,
                "visibility": Event.Visibility.PUBLIC.value,
                "visibility_settings": {
                    "show_attendee_count": False,
                    "show_capacity": False,
                    "show_attendee_list": False,
                },
            },
            content_type="application/json",
        )

        assert response.status_code in (200, 201), response.content
        event = Event.objects.get(id=response.json()["id"])
        assert event.visibility_flags.show_attendee_count is False
        assert event.visibility_flags.show_capacity is False
        assert event.visibility_flags.show_attendee_list is False

    def test_edit_persists_the_settings(self, organization_owner_client: Client, public_event: Event) -> None:
        """PUT round-trips the nested object through ``update_db_instance``."""
        response = organization_owner_client.put(
            reverse("api:edit_event", kwargs={"event_id": str(public_event.id)}),
            data={"visibility_settings": {"show_capacity": False}},
            content_type="application/json",
        )

        assert response.status_code == 200, response.content
        public_event.refresh_from_db()
        assert public_event.visibility_flags.show_capacity is False
        assert public_event.visibility_flags.show_attendee_count is True

    def test_partial_edit_does_not_re_enable_hidden_toggles(
        self, organization_owner_client: Client, public_event: Event
    ) -> None:
        """Naming one toggle must not silently re-disclose the others.

        ``exclude_unset`` propagates into the nested model, so writing the sent
        blob verbatim would replace the stored one — turning a previously hidden
        attendee count back on as a side effect of editing ``show_capacity``.
        """
        _set_visibility(public_event, show_attendee_count=False)

        response = organization_owner_client.put(
            reverse("api:edit_event", kwargs={"event_id": str(public_event.id)}),
            data={"visibility_settings": {"show_capacity": False}},
            content_type="application/json",
        )

        assert response.status_code == 200, response.content
        public_event.refresh_from_db()
        assert public_event.visibility_flags.show_attendee_count is False
        assert public_event.visibility_flags.show_capacity is False

    def test_partial_edit_can_still_re_enable_a_toggle(
        self, organization_owner_client: Client, public_event: Event
    ) -> None:
        """Merging hides nothing the organizer deliberately turns back on."""
        _set_visibility(public_event, show_attendee_count=False, show_capacity=False)

        response = organization_owner_client.put(
            reverse("api:edit_event", kwargs={"event_id": str(public_event.id)}),
            data={"visibility_settings": {"show_attendee_count": True}},
            content_type="application/json",
        )

        assert response.status_code == 200, response.content
        public_event.refresh_from_db()
        assert public_event.visibility_flags.show_attendee_count is True
        assert public_event.visibility_flags.show_capacity is False

    def test_default_equivalent_write_does_not_detach_an_occurrence(
        self, organization_owner_client: Client, public_event: Event
    ) -> None:
        """``{}`` and an explicit all-defaults blob mean the same thing.

        A frontend that round-trips the whole settings object would otherwise
        flip ``is_modified`` on its first save, permanently cutting the
        occurrence off from template propagation over a no-op edit.
        """
        from events.models import EventSeries

        series = EventSeries.objects.create(organization=public_event.organization, name="Series")
        public_event.event_series = series
        public_event.occurrence_index = 1
        public_event.is_modified = False
        public_event.visibility_settings = {}
        public_event.save()

        response = organization_owner_client.put(
            reverse("api:edit_event", kwargs={"event_id": str(public_event.id)}),
            data={"visibility_settings": {"show_attendee_count": True, "show_capacity": True}},
            content_type="application/json",
        )

        assert response.status_code == 200, response.content
        public_event.refresh_from_db()
        assert public_event.is_modified is False

    def test_a_real_visibility_change_does_mark_an_occurrence_modified(
        self, organization_owner_client: Client, public_event: Event
    ) -> None:
        from events.models import EventSeries

        series = EventSeries.objects.create(organization=public_event.organization, name="Series")
        public_event.event_series = series
        public_event.occurrence_index = 1
        public_event.is_modified = False
        public_event.save()

        response = organization_owner_client.put(
            reverse("api:edit_event", kwargs={"event_id": str(public_event.id)}),
            data={"visibility_settings": {"show_capacity": False}},
            content_type="application/json",
        )

        assert response.status_code == 200, response.content
        public_event.refresh_from_db()
        assert public_event.is_modified is True

    def test_edit_without_the_field_leaves_it_untouched(
        self, organization_owner_client: Client, public_event: Event
    ) -> None:
        """``exclude_unset`` protects a client that does not know about the field yet."""
        _set_visibility(public_event, show_attendee_list=False)

        response = organization_owner_client.put(
            reverse("api:edit_event", kwargs={"event_id": str(public_event.id)}),
            data={"name": "Renamed"},
            content_type="application/json",
        )

        assert response.status_code == 200, response.content
        public_event.refresh_from_db()
        assert public_event.visibility_flags.show_attendee_list is False

    def test_unknown_toggle_is_rejected(self, organization_owner_client: Client, organization: Organization) -> None:
        response = organization_owner_client.post(
            reverse("api:create_event", kwargs={"slug": organization.slug}),
            data={
                "name": "Bad Event",
                "start": "2099-01-01T18:00:00Z",
                "event_type": Event.EventType.PUBLIC.value,
                "visibility": Event.Visibility.PUBLIC.value,
                "visibility_settings": {"show_everything": True},
            },
            content_type="application/json",
        )

        assert response.status_code == 422, response.content
