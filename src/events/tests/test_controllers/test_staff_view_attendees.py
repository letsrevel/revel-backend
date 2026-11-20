"""Staff views of attendees (list, dietary summary, pronoun distribution) exclude unknown holders.

An account whose only link to the organization is staff-originated (box-office ticket, staff RSVP) is
"unknown": staff must not see its profile, dietary data or pronouns.
"""

import pytest
from django.test.client import Client
from django.urls import reverse

from accounts.models import DietaryRestriction, FoodItem, RevelUser
from conftest import RevelUserFactory
from events.models import Event, EventRSVP, OrganizationMember, Ticket, TicketSaleSource, TicketTier

pytestmark = pytest.mark.django_db


@pytest.fixture
def tier(event: Event) -> TicketTier:
    """A free tier on the event."""
    return TicketTier.objects.create(
        event=event,
        name="General",
        visibility=TicketTier.Visibility.PUBLIC,
        payment_method=TicketTier.PaymentMethod.FREE,
        price=0,
    )


def _ticket(event: Event, tier: TicketTier, user: RevelUser, source: TicketSaleSource | None = None) -> Ticket:
    return Ticket.objects.create(
        guest_name="Guest",
        event=event,
        user=user,
        tier=tier,
        status=Ticket.TicketStatus.ACTIVE,
        sale_source=source,
    )


def _names(client: Client, event: Event) -> set[str]:
    response = client.get(reverse("api:event_attendee_list", kwargs={"event_id": event.id}))
    assert response.status_code == 200
    return {r["first_name"] for r in response.json()["results"]}


class TestAttendeeList:
    def test_owner_does_not_see_box_office_holder(
        self,
        organization_owner_client: Client,
        event: Event,
        tier: TicketTier,
        revel_user_factory: RevelUserFactory,
    ) -> None:
        comp = revel_user_factory(first_name="Comp")
        buyer = revel_user_factory(first_name="Buyer")
        member = revel_user_factory(first_name="Member")
        _ticket(event, tier, comp, TicketSaleSource.BOX_OFFICE_COMP)
        _ticket(event, tier, buyer)
        _ticket(event, tier, member, TicketSaleSource.BOX_OFFICE_SALE)
        OrganizationMember.objects.create(organization=event.organization, user=member)

        assert _names(organization_owner_client, event) == {"Buyer", "Member"}

    def test_staff_rsvp_excluded_and_returns_once_known(
        self,
        organization_staff_client: Client,
        event: Event,
        tier: TicketTier,
        revel_user_factory: RevelUserFactory,
    ) -> None:
        guest = revel_user_factory(first_name="Staffrsvp")
        rsvp = EventRSVP.objects.create(event=event, user=guest, status=EventRSVP.RsvpStatus.YES)
        EventRSVP.objects.filter(pk=rsvp.pk).update(source=EventRSVP.Source.STAFF)
        assert _names(organization_staff_client, event) == set()

        # The account's own RSVP makes it known.
        EventRSVP.objects.filter(pk=rsvp.pk).update(source=EventRSVP.Source.SELF)
        assert _names(organization_staff_client, event) == {"Staffrsvp"}

    def test_comp_holder_visible_after_becoming_member(
        self,
        organization_owner_client: Client,
        event: Event,
        tier: TicketTier,
        revel_user_factory: RevelUserFactory,
    ) -> None:
        comp = revel_user_factory(first_name="Comp")
        _ticket(event, tier, comp, TicketSaleSource.BOX_OFFICE_COMP)
        assert _names(organization_owner_client, event) == set()

        OrganizationMember.objects.create(organization=event.organization, user=comp)
        assert _names(organization_owner_client, event) == {"Comp"}


class TestDietarySummary:
    def test_unknown_holder_restriction_and_note_excluded(
        self,
        organization_owner_client: Client,
        event: Event,
        tier: TicketTier,
        revel_user_factory: RevelUserFactory,
    ) -> None:
        unknown = revel_user_factory()
        known = revel_user_factory()
        _ticket(event, tier, unknown, TicketSaleSource.BOX_OFFICE_COMP)
        _ticket(event, tier, known)
        peanuts, _ = FoodItem.objects.get_or_create(name="Peanuts")
        gluten, _ = FoodItem.objects.get_or_create(name="Gluten")
        DietaryRestriction.objects.create(
            user=unknown,
            food_item=gluten,
            restriction_type=DietaryRestriction.RestrictionType.ALLERGY,
            notes="unknown-private-note",
            is_public=False,
        )
        DietaryRestriction.objects.create(
            user=known,
            food_item=peanuts,
            restriction_type=DietaryRestriction.RestrictionType.ALLERGY,
            notes="known-note",
            is_public=False,
        )
        url = reverse("api:event_dietary_summary", args=[event.id])

        data = organization_owner_client.get(url).json()
        assert [r["food_item"] for r in data["restrictions"]] == ["Peanuts"]
        assert "unknown-private-note" not in str(data)
        assert data["restrictions"][0]["notes"] == ["known-note"]

        OrganizationMember.objects.create(organization=event.organization, user=unknown)
        data = organization_owner_client.get(url).json()
        assert {r["food_item"] for r in data["restrictions"]} == {"Peanuts", "Gluten"}


class TestPronounDistribution:
    def test_unknown_holder_excluded_and_returns_once_known(
        self,
        organization_owner_client: Client,
        event: Event,
        tier: TicketTier,
        revel_user_factory: RevelUserFactory,
    ) -> None:
        unknown = revel_user_factory()
        known = revel_user_factory()
        RevelUser.objects.filter(pk=unknown.pk).update(pronouns="xe/xem")
        RevelUser.objects.filter(pk=known.pk).update(pronouns="she/her")
        _ticket(event, tier, unknown, TicketSaleSource.BOX_OFFICE_SALE)
        _ticket(event, tier, known)
        url = reverse("api:event_pronoun_distribution", args=[event.id])

        data = organization_owner_client.get(url).json()
        assert data["distribution"] == [{"pronouns": "she/her", "count": 1}]
        assert data["total_attendees"] == 1

        OrganizationMember.objects.create(organization=event.organization, user=unknown)
        data = organization_owner_client.get(url).json()
        assert {d["pronouns"] for d in data["distribution"]} == {"she/her", "xe/xem"}
        assert data["total_attendees"] == 2
