"""TicketTier.grants_membership_tier validation and plumbing (#1320)."""

import typing as t
import uuid
from datetime import timedelta

import orjson
import pytest
from django.test.client import Client
from django.urls import reverse
from django.utils import timezone

from accounts.models import RevelUser
from events.models import Event, MembershipTier, Organization, TicketTier
from events.service.duplication import duplicate_event

pytestmark = pytest.mark.django_db


def _json(client: Client, method: str, url: str, payload: dict[str, t.Any]) -> t.Any:
    return getattr(client, method)(url, data=orjson.dumps(payload), content_type="application/json")


def _create(client: Client, event: Event, **extra: t.Any) -> t.Any:
    url = reverse("api:create_ticket_tier", kwargs={"event_id": event.pk})
    payload = {"name": "Day ticket", "price": "0", "currency": "EUR", "max_tickets_per_user": 1, **extra}
    return _json(client, "post", url, payload)


def _messages(response: t.Any) -> dict[str, list[str]]:
    """Field -> messages of a validation error, unwrapped from the handler's ``errors`` envelope."""
    body = response.json()
    errors = body.get("errors")
    if not isinstance(errors, dict):
        raise AssertionError(f"expected field errors, got {body!r}")
    return t.cast(dict[str, list[str]], errors)


class TestCreate:
    def test_grant_succeeds_and_is_exposed(
        self, organization_owner_client: Client, event: Event, temp_tier: MembershipTier
    ) -> None:
        response = _create(organization_owner_client, event, grants_membership_tier_id=str(temp_tier.id))
        assert response.status_code == 200, response.content
        assert response.json()["grants_membership_tier_id"] == str(temp_tier.id)
        assert TicketTier.objects.get(pk=response.json()["id"]).grants_membership_tier == temp_tier

    def test_no_grant_is_null(self, organization_owner_client: Client, event: Event) -> None:
        response = _create(organization_owner_client, event)
        assert response.status_code == 200
        assert response.json()["grants_membership_tier_id"] is None

    def test_non_members_with_exclusions_is_allowed(
        self,
        organization_owner_client: Client,
        event: Event,
        temp_tier: MembershipTier,
        standard_tier: MembershipTier,
    ) -> None:
        """The Verein day ticket: non-members only, optionally excluding some member tiers."""
        response = _create(
            organization_owner_client,
            event,
            grants_membership_tier_id=str(temp_tier.id),
            purchasable_by="non_members",
            excluded_membership_tiers_ids=[str(standard_tier.id)],
        )
        assert response.status_code == 200, response.content

    def test_standard_tier_is_rejected(
        self, organization_owner_client: Client, event: Event, standard_tier: MembershipTier
    ) -> None:
        response = _create(organization_owner_client, event, grants_membership_tier_id=str(standard_tier.id))
        assert response.status_code == 400
        assert _messages(response) == {"grants_membership_tier": ["Only a temporary tier can be granted by tickets."]}

    def test_other_organization_tier_is_rejected(
        self, organization_owner_client: Client, event: Event, organization_owner_user: t.Any
    ) -> None:
        other = Organization.objects.create(name="Other", slug="other-org", owner=organization_owner_user)
        foreign = MembershipTier.objects.create(organization=other, name="Day", kind=MembershipTier.Kind.TEMPORARY)
        response = _create(organization_owner_client, event, grants_membership_tier_id=str(foreign.id))
        assert response.status_code == 400
        assert _messages(response) == {
            "grants_membership_tier": ["The granted membership tier must belong to the event's organization."]
        }

    def test_unknown_id_is_a_client_error_not_a_500(self, organization_owner_client: Client, event: Event) -> None:
        response = _create(organization_owner_client, event, grants_membership_tier_id=str(uuid.uuid4()))
        assert response.status_code == 400
        assert _messages(response) == {
            "grants_membership_tier": ["The granted membership tier must belong to the event's organization."]
        }

    @pytest.mark.parametrize("purchasable_by", ["members", "invited_and_members"])
    def test_member_gated_purchasable_by_is_rejected(
        self, organization_owner_client: Client, event: Event, temp_tier: MembershipTier, purchasable_by: str
    ) -> None:
        response = _create(
            organization_owner_client,
            event,
            grants_membership_tier_id=str(temp_tier.id),
            purchasable_by=purchasable_by,
        )
        assert response.status_code == 400
        assert "purchasable_by" in response.content.decode()

    @pytest.mark.parametrize("cap", [None, 2])
    def test_max_tickets_per_user_must_be_one(
        self, organization_owner_client: Client, event: Event, temp_tier: MembershipTier, cap: int | None
    ) -> None:
        response = _create(
            organization_owner_client, event, grants_membership_tier_id=str(temp_tier.id), max_tickets_per_user=cap
        )
        assert response.status_code == 400
        assert "max_tickets_per_user" in response.content.decode()

    def test_restricted_to_membership_tiers_is_rejected(
        self,
        organization_owner_client: Client,
        event: Event,
        temp_tier: MembershipTier,
        standard_tier: MembershipTier,
    ) -> None:
        response = _create(
            organization_owner_client,
            event,
            grants_membership_tier_id=str(temp_tier.id),
            purchasable_by="members",
            restricted_to_membership_tiers_ids=[str(standard_tier.id)],
        )
        assert response.status_code == 400
        assert "purchasable_by" in _messages(response)
        assert not TicketTier.objects.filter(event=event, name="Day ticket").exists()


class TestUpdate:
    @pytest.fixture
    def granting_tier(self, event: Event, temp_tier: MembershipTier) -> TicketTier:
        return TicketTier.objects.create(
            event=event, name="Day ticket", max_tickets_per_user=1, grants_membership_tier=temp_tier
        )

    def _url(self, tier: TicketTier) -> str:
        return reverse("api:update_ticket_tier", kwargs={"event_id": tier.event_id, "tier_id": tier.pk})

    def test_set_grant_on_existing_tier(
        self, organization_owner_client: Client, event: Event, temp_tier: MembershipTier
    ) -> None:
        tier = TicketTier.objects.create(event=event, name="Plain", max_tickets_per_user=1)
        response = _json(
            organization_owner_client, "put", self._url(tier), {"grants_membership_tier_id": str(temp_tier.id)}
        )
        assert response.status_code == 200, response.content
        tier.refresh_from_db()
        assert tier.grants_membership_tier == temp_tier

    def test_omitted_keeps_grant(self, organization_owner_client: Client, granting_tier: TicketTier) -> None:
        response = _json(organization_owner_client, "put", self._url(granting_tier), {"description": "x"})
        assert response.status_code == 200
        granting_tier.refresh_from_db()
        assert granting_tier.grants_membership_tier_id is not None

    def test_null_clears_grant(self, organization_owner_client: Client, granting_tier: TicketTier) -> None:
        response = _json(
            organization_owner_client, "put", self._url(granting_tier), {"grants_membership_tier_id": None}
        )
        assert response.status_code == 200
        assert response.json()["grants_membership_tier_id"] is None
        granting_tier.refresh_from_db()
        assert granting_tier.grants_membership_tier_id is None

    def test_cannot_lift_the_one_ticket_cap_while_granting(
        self, organization_owner_client: Client, granting_tier: TicketTier
    ) -> None:
        response = _json(organization_owner_client, "put", self._url(granting_tier), {"max_tickets_per_user": 3})
        assert response.status_code == 400

    def test_cannot_switch_to_members_only_while_granting(
        self, organization_owner_client: Client, granting_tier: TicketTier
    ) -> None:
        response = _json(organization_owner_client, "put", self._url(granting_tier), {"purchasable_by": "members"})
        assert response.status_code == 400


class TestModelAndCopy:
    def test_admin_picker_offers_only_temporary_tiers(
        self, event: Event, temp_tier: MembershipTier, standard_tier: MembershipTier, superuser: RevelUser
    ) -> None:
        from django.contrib import admin
        from django.test import RequestFactory

        def _admin_request(user: RevelUser) -> t.Any:
            request = RequestFactory().get("/")
            request.user = user
            return request

        model_admin = admin.site._registry[TicketTier]
        field = TicketTier._meta.get_field("grants_membership_tier")
        formfield = t.cast(t.Any, model_admin.formfield_for_foreignkey(t.cast(t.Any, field), _admin_request(superuser)))
        offered = set(formfield.queryset)
        assert temp_tier in offered
        assert standard_tier not in offered

    def test_duplicate_event_copies_the_grant(self, event: Event, temp_tier: MembershipTier) -> None:
        TicketTier.objects.create(
            event=event, name="Day ticket", max_tickets_per_user=1, grants_membership_tier=temp_tier
        )
        copy = duplicate_event(event, "Copy", event.start + timedelta(days=7))
        copied = copy.ticket_tiers.get(name="Day ticket")
        assert copied.grants_membership_tier == temp_tier
        assert timezone.now() < copy.start

    def test_deleting_the_temporary_tier_clears_the_grant(self, event: Event, temp_tier: MembershipTier) -> None:
        tier = TicketTier.objects.create(
            event=event, name="Day ticket", max_tickets_per_user=1, grants_membership_tier=temp_tier
        )
        temp_tier.delete()
        tier.refresh_from_db()
        assert tier.grants_membership_tier_id is None
