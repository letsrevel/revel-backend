"""Controller tests for virtual-event VAT preview and place-of-supply fields (#868/#869).

Covers:
- POST /events/{event_id}/tickets/vat-preview on a virtual event — reverse charge
  for cross-border EU B2B, the B2C interim disclaimer, and the physical control.
- POST /organization-admin/{slug}/create-event and PUT /event-admin/{event_id} —
  ``is_virtual`` + ``vat_country_code`` round-trip, country-code validation, and
  the ``effective_vat_country`` / ``vat_country_mismatch`` detail fields.
"""

from decimal import Decimal
from unittest.mock import MagicMock, patch

import orjson
import pytest
from django.contrib.gis.geos import Point
from django.test.client import Client
from django.urls import reverse
from django.utils import timezone

from events.models import Event, Organization, TicketTier
from geo.models import City

pytestmark = pytest.mark.django_db

MOCK_VIES = "common.service.vies_service.validate_vat_id_cached"


def _make_org_invoicing_ready(org: Organization) -> Organization:
    """Configure an org with all prerequisites for invoicing (IT seller, 22%)."""
    org.vat_country_code = "IT"
    org.vat_id = "IT12345678901"
    org.vat_id_validated = True
    org.vat_rate = Decimal("22.00")
    org.billing_name = "ACME SRL"
    org.billing_address = "Via Roma 1, 00100 Roma"
    org.billing_email = "billing@acme.it"
    org.contact_email = "info@acme.it"
    org.save()
    return org


def _make_berlin() -> City:
    """A German city for cross-country place-of-supply setups."""
    return City.objects.create(
        name="Berlin",
        ascii_name="Berlin",
        country="Germany",
        iso2="DE",
        iso3="DEU",
        city_id=910001,
        location=Point(13.40, 52.52),
        population=3600000,
    )


# ---------------------------------------------------------------------------
# POST /events/{event_id}/tickets/vat-preview — virtual events
# ---------------------------------------------------------------------------


class TestVirtualEventVATPreview:
    """VAT preview on a virtual event (is_virtual=True)."""

    @pytest.fixture
    def virtual_event(self, event: Event, organization: Organization) -> Event:
        """The generic event, made virtual, on an invoicing-ready IT org."""
        _make_org_invoicing_ready(organization)
        event.is_virtual = True
        event.save(update_fields=["is_virtual"])
        return event

    @patch(MOCK_VIES)
    def test_eu_cross_border_b2b_validated_is_reverse_charged(
        self,
        mock_vies: MagicMock,
        organization_owner_client: Client,
        virtual_event: Event,
        event_ticket_tier: TicketTier,
    ) -> None:
        """A validated DE VAT ID on a virtual IT event reverse-charges: totals are net."""
        from common.service.vies_service import VIESValidationResult

        mock_vies.return_value = VIESValidationResult(
            valid=True, name="Buyer GmbH", address="Berlin", request_identifier="R1"
        )
        url = reverse("api:vat_preview", kwargs={"event_id": str(virtual_event.id)})

        response = organization_owner_client.post(
            url,
            data=orjson.dumps(
                {
                    "billing_info": {
                        "billing_name": "Buyer GmbH",
                        "vat_id": "DE123456789",
                        "vat_country_code": "DE",
                    },
                    "items": [{"tier_id": str(event_ticket_tier.id), "count": 1}],
                }
            ),
            content_type="application/json",
        )

        assert response.status_code == 200
        data = response.json()
        assert data["vat_id_valid"] is True
        assert data["reverse_charge"] is True
        assert data["virtual_b2c_disclaimer"] is False
        # Tier gross 10.00 at 22% -> net 8.20; the buyer is charged the net.
        assert Decimal(data["total_gross"]) == Decimal("8.20")
        assert Decimal(data["total_net"]) == Decimal("8.20")
        assert Decimal(data["total_vat"]) == Decimal("0.00")

    @patch(MOCK_VIES)
    def test_eu_cross_border_b2c_gets_interim_disclaimer_and_full_vat(
        self,
        mock_vies: MagicMock,
        organization_owner_client: Client,
        virtual_event: Event,
        event_ticket_tier: TicketTier,
    ) -> None:
        """A DE consumer (no VAT ID) pays full VAT at the IT rate, with the disclaimer."""
        url = reverse("api:vat_preview", kwargs={"event_id": str(virtual_event.id)})

        response = organization_owner_client.post(
            url,
            data=orjson.dumps(
                {
                    "billing_info": {
                        "billing_name": "Max Mustermann",
                        "vat_country_code": "DE",
                    },
                    "items": [{"tier_id": str(event_ticket_tier.id), "count": 1}],
                }
            ),
            content_type="application/json",
        )

        assert response.status_code == 200
        data = response.json()
        assert data["reverse_charge"] is False
        assert data["virtual_b2c_disclaimer"] is True
        assert Decimal(data["total_gross"]) == Decimal("10.00")
        assert Decimal(data["total_vat"]) == Decimal("1.80")
        mock_vies.assert_not_called()

    @patch(MOCK_VIES)
    def test_physical_event_same_b2b_context_stays_gross(
        self,
        mock_vies: MagicMock,
        organization_owner_client: Client,
        organization: Organization,
        event: Event,
        event_ticket_tier: TicketTier,
    ) -> None:
        """The same validated B2B buyer on the physical event pays gross, no disclaimer (#868)."""
        from common.service.vies_service import VIESValidationResult

        _make_org_invoicing_ready(organization)
        assert event.is_virtual is False
        mock_vies.return_value = VIESValidationResult(
            valid=True, name="Buyer GmbH", address="Berlin", request_identifier="R2"
        )
        url = reverse("api:vat_preview", kwargs={"event_id": str(event.id)})

        response = organization_owner_client.post(
            url,
            data=orjson.dumps(
                {
                    "billing_info": {
                        "billing_name": "Buyer GmbH",
                        "vat_id": "DE123456789",
                        "vat_country_code": "DE",
                    },
                    "items": [{"tier_id": str(event_ticket_tier.id), "count": 1}],
                }
            ),
            content_type="application/json",
        )

        assert response.status_code == 200
        data = response.json()
        assert data["vat_id_valid"] is True
        assert data["reverse_charge"] is False
        assert data["virtual_b2c_disclaimer"] is False
        assert Decimal(data["total_gross"]) == Decimal("10.00")
        assert Decimal(data["total_vat"]) == Decimal("1.80")


# ---------------------------------------------------------------------------
# Event create/update — is_virtual + vat_country_code
# ---------------------------------------------------------------------------


class TestEventCreatePlaceOfSupplyFields:
    """POST /organization-admin/{slug}/create-event with the new VAT fields."""

    @staticmethod
    def _payload(**extra: object) -> bytes:
        base: dict[str, object] = {
            "name": "Virtual Summit",
            "event_type": "public",
            "visibility": "public",
            "status": "open",
            "start": timezone.now().isoformat(),
        }
        base.update(extra)
        return orjson.dumps(base)

    def test_create_accepts_is_virtual_and_vat_country_code(
        self, organization_owner_client: Client, organization: Organization
    ) -> None:
        """Both fields persist and echo back in the EventDetailSchema response."""
        url = reverse("api:create_event", kwargs={"slug": organization.slug})

        response = organization_owner_client.post(
            url,
            data=self._payload(is_virtual=True, vat_country_code="DE"),
            content_type="application/json",
        )

        assert response.status_code == 200
        data = response.json()
        assert data["is_virtual"] is True
        assert data["vat_country_code"] == "DE"
        assert data["effective_vat_country"] == "DE"
        created = Event.objects.get(id=data["id"])
        assert created.is_virtual is True
        assert created.vat_country_code == "DE"

    def test_create_rejects_invalid_country_code(
        self, organization_owner_client: Client, organization: Organization
    ) -> None:
        """ "XX" is not an ISO 3166-1 alpha-2 code — pydantic rejects it with 422."""
        url = reverse("api:create_event", kwargs={"slug": organization.slug})

        response = organization_owner_client.post(
            url,
            data=self._payload(vat_country_code="XX"),
            content_type="application/json",
        )

        assert response.status_code == 422
        assert not Event.objects.filter(name="Virtual Summit").exists()

    def test_create_accepts_lowercase_country_code(
        self, organization_owner_client: Client, organization: Organization
    ) -> None:
        """Lowercase codes validate (comparison is case-insensitive) and are stored uppercased."""
        url = reverse("api:create_event", kwargs={"slug": organization.slug})

        response = organization_owner_client.post(
            url,
            data=self._payload(vat_country_code="de"),
            content_type="application/json",
        )

        assert response.status_code == 200
        data = response.json()
        assert data["vat_country_code"] == "DE"
        assert data["effective_vat_country"] == "DE"

    def test_create_defaults_to_physical_no_override(
        self, organization_owner_client: Client, organization: Organization
    ) -> None:
        """Omitting both fields yields a physical event with no VAT override."""
        url = reverse("api:create_event", kwargs={"slug": organization.slug})

        response = organization_owner_client.post(url, data=self._payload(), content_type="application/json")

        assert response.status_code == 200
        data = response.json()
        assert data["is_virtual"] is False
        assert data["vat_country_code"] == ""


class TestEventUpdatePlaceOfSupplyFields:
    """PUT /event-admin/{event_id} with the new VAT fields."""

    def test_update_sets_is_virtual_and_vat_country_code(self, organization_owner_client: Client, event: Event) -> None:
        """Both fields are editable and round-trip through the detail response."""
        url = reverse("api:edit_event", kwargs={"event_id": event.pk})

        response = organization_owner_client.put(
            url,
            data=orjson.dumps({"is_virtual": True, "vat_country_code": "FR"}),
            content_type="application/json",
        )

        assert response.status_code == 200
        data = response.json()
        assert data["is_virtual"] is True
        assert data["vat_country_code"] == "FR"
        assert data["effective_vat_country"] == "FR"
        event.refresh_from_db()
        assert event.is_virtual is True
        assert event.vat_country_code == "FR"

    def test_update_rejects_invalid_country_code(self, organization_owner_client: Client, event: Event) -> None:
        """ "XX" on update is rejected with 422 and nothing persists."""
        url = reverse("api:edit_event", kwargs={"event_id": event.pk})

        response = organization_owner_client.put(
            url,
            data=orjson.dumps({"vat_country_code": "XX"}),
            content_type="application/json",
        )

        assert response.status_code == 422
        event.refresh_from_db()
        assert event.vat_country_code == ""


class TestVatCountryMismatchFlag:
    """EventDetailSchema.vat_country_mismatch resolver."""

    def _detail(self, client: Client, event: Event) -> dict[str, object]:
        """Fetch the detail body via a no-op PUT (returns EventDetailSchema)."""
        url = reverse("api:edit_event", kwargs={"event_id": event.pk})
        response = client.put(url, data=orjson.dumps({"name": event.name}), content_type="application/json")
        assert response.status_code == 200
        return dict(response.json())

    def test_mismatch_true_for_physical_event_in_another_country(
        self, organization_owner_client: Client, organization: Organization, event: Event
    ) -> None:
        """Physical event whose city's country differs from the org's VAT country flags True."""
        organization.vat_country_code = "IT"
        organization.save(update_fields=["vat_country_code"])
        event.city = _make_berlin()
        event.save(update_fields=["city"])

        data = self._detail(organization_owner_client, event)

        assert data["effective_vat_country"] == "DE"
        assert data["vat_country_mismatch"] is True

    def test_mismatch_false_when_event_is_virtual(
        self, organization_owner_client: Client, organization: Organization, event: Event
    ) -> None:
        """Virtual events are supplied from the org's establishment — never flagged."""
        organization.vat_country_code = "IT"
        organization.save(update_fields=["vat_country_code"])
        event.city = _make_berlin()
        event.is_virtual = True
        event.save(update_fields=["city", "is_virtual"])

        data = self._detail(organization_owner_client, event)

        assert data["vat_country_mismatch"] is False

    def test_mismatch_false_when_org_has_no_vat_country(
        self, organization_owner_client: Client, organization: Organization, event: Event
    ) -> None:
        """No org VAT country means nothing to mismatch against."""
        assert organization.vat_country_code == ""
        event.city = _make_berlin()
        event.save(update_fields=["city"])

        data = self._detail(organization_owner_client, event)

        assert data["vat_country_mismatch"] is False

    def test_mismatch_false_when_countries_agree(
        self, organization_owner_client: Client, organization: Organization, event: Event
    ) -> None:
        """A physical event in the org's own VAT country is not flagged."""
        organization.vat_country_code = "IT"
        organization.save(update_fields=["vat_country_code"])
        event.vat_country_code = "IT"
        event.save(update_fields=["vat_country_code"])

        data = self._detail(organization_owner_client, event)

        assert data["effective_vat_country"] == "IT"
        assert data["vat_country_mismatch"] is False
