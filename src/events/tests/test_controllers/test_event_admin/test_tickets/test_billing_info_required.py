"""Tests for billing info requirement on online ticket tiers with platform fees."""

from decimal import Decimal

import orjson
import pytest
from django.test.client import Client
from django.urls import reverse

from events.models import Event, Organization, TicketTier

pytestmark = pytest.mark.django_db


def _make_stripe_connected(org: Organization) -> None:
    """Enable Stripe Connect on an organization."""
    org.stripe_account_id = "acct_test_123"
    org.stripe_charges_enabled = True
    org.stripe_details_submitted = True
    org.save(update_fields=["stripe_account_id", "stripe_charges_enabled", "stripe_details_submitted"])


def _set_billing_info(org: Organization, *, country: str = "IT", address: str = "Via Roma 1, 00100 Roma") -> None:
    """Set billing info on an organization."""
    org.vat_country_code = country
    org.billing_address = address
    org.save(update_fields=["vat_country_code", "billing_address"])


# ===========================================================================
# CREATE ticket tier
# ===========================================================================


class TestCreateOnlineTierBillingRequired:
    """Test billing info requirement when creating online ticket tiers."""

    def test_create_online_tier_rejected_without_billing_info(
        self,
        organization_owner_client: Client,
        event: Event,
        organization: Organization,
    ) -> None:
        """Online tier with platform fees should be rejected when billing info is missing."""
        _make_stripe_connected(organization)
        assert organization.platform_fee_percent > 0  # sanity check

        url = reverse("api:create_ticket_tier", kwargs={"event_id": event.pk})
        payload = {"name": "Online Tier", "price": "25.00", "payment_method": "online"}

        response = organization_owner_client.post(url, data=orjson.dumps(payload), content_type="application/json")

        assert response.status_code == 400
        assert "billing" in response.json()["detail"].lower()

    def test_create_online_tier_rejected_without_country(
        self,
        organization_owner_client: Client,
        event: Event,
        organization: Organization,
    ) -> None:
        """Online tier should be rejected when only billing_address is set (no country)."""
        _make_stripe_connected(organization)
        organization.billing_address = "Via Roma 1"
        organization.save(update_fields=["billing_address"])

        url = reverse("api:create_ticket_tier", kwargs={"event_id": event.pk})
        payload = {"name": "Online Tier", "price": "25.00", "payment_method": "online"}

        response = organization_owner_client.post(url, data=orjson.dumps(payload), content_type="application/json")

        assert response.status_code == 400

    def test_create_online_tier_rejected_without_address(
        self,
        organization_owner_client: Client,
        event: Event,
        organization: Organization,
    ) -> None:
        """Online tier should be rejected when only vat_country_code is set (no address)."""
        _make_stripe_connected(organization)
        organization.vat_country_code = "IT"
        organization.save(update_fields=["vat_country_code"])

        url = reverse("api:create_ticket_tier", kwargs={"event_id": event.pk})
        payload = {"name": "Online Tier", "price": "25.00", "payment_method": "online"}

        response = organization_owner_client.post(url, data=orjson.dumps(payload), content_type="application/json")

        assert response.status_code == 400

    def test_create_online_tier_allowed_with_billing_info(
        self,
        organization_owner_client: Client,
        event: Event,
        organization: Organization,
    ) -> None:
        """Online tier should succeed when billing info is complete."""
        _make_stripe_connected(organization)
        _set_billing_info(organization)

        url = reverse("api:create_ticket_tier", kwargs={"event_id": event.pk})
        payload = {"name": "Online Tier", "price": "25.00", "payment_method": "online"}

        response = organization_owner_client.post(url, data=orjson.dumps(payload), content_type="application/json")

        assert response.status_code == 200
        assert response.json()["payment_method"] == "online"

    def test_create_online_tier_allowed_with_zero_platform_fees(
        self,
        organization_owner_client: Client,
        event: Event,
        organization: Organization,
    ) -> None:
        """Online tier should succeed without billing info when platform fees are zero."""
        _make_stripe_connected(organization)
        organization.platform_fee_percent = Decimal("0.00")
        organization.platform_fee_fixed = Decimal("0.00")
        organization.save(update_fields=["platform_fee_percent", "platform_fee_fixed"])

        url = reverse("api:create_ticket_tier", kwargs={"event_id": event.pk})
        payload = {"name": "Online Tier", "price": "25.00", "payment_method": "online"}

        response = organization_owner_client.post(url, data=orjson.dumps(payload), content_type="application/json")

        assert response.status_code == 200

    def test_create_offline_tier_allowed_without_billing_info(
        self,
        organization_owner_client: Client,
        event: Event,
        organization: Organization,
    ) -> None:
        """Non-online tiers should not require billing info regardless of platform fees."""
        assert organization.platform_fee_percent > 0  # sanity check

        url = reverse("api:create_ticket_tier", kwargs={"event_id": event.pk})
        payload = {"name": "Offline Tier", "price": "25.00", "payment_method": "offline"}

        response = organization_owner_client.post(url, data=orjson.dumps(payload), content_type="application/json")

        assert response.status_code == 200

    def test_create_free_tier_allowed_without_billing_info(
        self,
        organization_owner_client: Client,
        event: Event,
    ) -> None:
        """Free tiers should not require billing info."""
        url = reverse("api:create_ticket_tier", kwargs={"event_id": event.pk})
        payload = {"name": "Free Tier", "price": "0.00", "payment_method": "free"}

        response = organization_owner_client.post(url, data=orjson.dumps(payload), content_type="application/json")

        assert response.status_code == 200


# ===========================================================================
# UPDATE ticket tier
# ===========================================================================


class TestUpdateTierBillingRequired:
    """Test billing info requirement when updating a ticket tier to online."""

    def test_update_to_online_rejected_without_billing_info(
        self,
        organization_owner_client: Client,
        event: Event,
        organization: Organization,
        event_ticket_tier: TicketTier,
    ) -> None:
        """Changing payment_method to online should be rejected without billing info."""
        _make_stripe_connected(organization)

        url = reverse("api:update_ticket_tier", kwargs={"event_id": event.pk, "tier_id": event_ticket_tier.pk})
        payload = {"payment_method": "online"}

        response = organization_owner_client.put(url, data=orjson.dumps(payload), content_type="application/json")

        assert response.status_code == 400
        assert "billing" in response.json()["detail"].lower()

    def test_update_to_online_allowed_with_billing_info(
        self,
        organization_owner_client: Client,
        event: Event,
        organization: Organization,
        event_ticket_tier: TicketTier,
    ) -> None:
        """Changing payment_method to online should succeed with complete billing info."""
        _make_stripe_connected(organization)
        _set_billing_info(organization)

        url = reverse("api:update_ticket_tier", kwargs={"event_id": event.pk, "tier_id": event_ticket_tier.pk})
        payload = {"payment_method": "online"}

        response = organization_owner_client.put(url, data=orjson.dumps(payload), content_type="application/json")

        assert response.status_code == 200
        assert response.json()["payment_method"] == "online"

    def test_update_non_payment_fields_allowed_without_billing_info(
        self,
        organization_owner_client: Client,
        event: Event,
        event_ticket_tier: TicketTier,
    ) -> None:
        """Updating fields other than payment_method should not trigger billing check."""
        url = reverse("api:update_ticket_tier", kwargs={"event_id": event.pk, "tier_id": event_ticket_tier.pk})
        payload = {"name": "Renamed Tier"}

        response = organization_owner_client.put(url, data=orjson.dumps(payload), content_type="application/json")

        assert response.status_code == 200
        assert response.json()["name"] == "Renamed Tier"

    def test_update_to_offline_allowed_without_billing_info(
        self,
        organization_owner_client: Client,
        event: Event,
        organization: Organization,
        event_ticket_tier: TicketTier,
    ) -> None:
        """Changing payment_method to offline should not require billing info."""
        url = reverse("api:update_ticket_tier", kwargs={"event_id": event.pk, "tier_id": event_ticket_tier.pk})
        payload = {"payment_method": "offline"}

        response = organization_owner_client.put(url, data=orjson.dumps(payload), content_type="application/json")

        assert response.status_code == 200

    def test_update_to_online_allowed_with_zero_platform_fees(
        self,
        organization_owner_client: Client,
        event: Event,
        organization: Organization,
        event_ticket_tier: TicketTier,
    ) -> None:
        """Changing to online should succeed without billing info when platform fees are zero."""
        _make_stripe_connected(organization)
        organization.platform_fee_percent = Decimal("0.00")
        organization.platform_fee_fixed = Decimal("0.00")
        organization.save(update_fields=["platform_fee_percent", "platform_fee_fixed"])

        url = reverse("api:update_ticket_tier", kwargs={"event_id": event.pk, "tier_id": event_ticket_tier.pk})
        payload = {"payment_method": "online"}

        response = organization_owner_client.put(url, data=orjson.dumps(payload), content_type="application/json")

        assert response.status_code == 200
