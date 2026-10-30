"""Attendee invoicing for small-business-scheme organizers without an EU VAT ID (#1244).

DE § 19 UStG and AT § 6 Abs. 1 Z 27 UStG: every line is E at 0% with the statutory note, reverse
charge never applies, and a virtual event sold to a business in another EU country is not invoiced
(recorded as skipped). An AT organizer is identified by the scheme, a tax registration number (BT-32)
and establishment in Austria instead of a VIES-validated VAT ID; a DE organizer must hold a
VIES-validated USt-IdNr. (#1258: Revel's platform fee to it is reverse-charged).
"""

import typing as t
from decimal import Decimal
from unittest.mock import patch

import orjson
import pytest
from django.db import connection
from django.template.loader import render_to_string
from django.test import Client
from django.urls import reverse
from ninja.errors import HttpError
from ninja_jwt.tokens import RefreshToken

from accounts.models import RevelUser
from common.service.vat_utils import b2b_vat_context
from events.compliance.registry import resolve_org_country
from events.models import Event, Organization, SkippedFiscalDocument, TicketTier
from events.service import small_business_scheme_service as service
from events.service.attendee_invoice_draft_service import update_draft_invoice
from events.service.attendee_invoice_service import (
    einvoice_missing_fields,
    generate_attendee_invoice,
    set_invoicing_mode,
    validate_invoicing_prerequisites,
)
from events.service.attendee_vat_service import determine_attendee_vat
from events.service.ticket_service import _assert_zero_rate_has_exemption_reason
from events.tests.test_attendee_invoice._helpers import (
    MOCK_RENDER_PDF,
    MOCK_SEND_EMAIL,
    _create_draft,
    _create_payment,
    _default_billing_snapshot,
    _make_org_invoicing_ready,
)
from events.utils import small_business

pytestmark = pytest.mark.django_db

DE_REASON = "Steuerbefreit gemäß § 19 UStG (Kleinunternehmerregelung)"
AT_REASON = "Umsatzsteuerbefreit – Kleinunternehmer gem. § 6 Abs. 1 Z 27 UStG"
Scheme = small_business.SmallBusinessScheme


def _make_small_business(org: Organization, scheme: str = Scheme.DE_19) -> Organization:
    """A Kleinunternehmer: structured DE/AT address, tax number, 0% rate.

    DE holds the VIES-validated USt-IdNr. the scheme requires (#1258); AT has no VAT ID or VAT country at all.
    """
    country = small_business.scheme_country(scheme)
    german = country == "DE"
    org.small_business_scheme = scheme
    org.tax_registration_number = "12/345/67890" if german else "12-345/6789"
    org.vat_id = "DE811234567" if german else ""
    org.vat_id_validated = german
    org.vat_country_code = "DE" if german else ""
    org.vat_rate = Decimal("0.00")
    org.vat_exemption_reason = small_business.scheme_exemption_reason(scheme)
    org.billing_name = "Kleine Bühne"
    org.billing_address = "Musterstraße 1, 10115 Berlin"
    org.billing_address_line1 = "Musterstraße 1"
    org.billing_postal_code = "10115" if country == "DE" else "1010"
    org.billing_city = "Berlin" if country == "DE" else "Wien"
    org.billing_country_code = country
    org.billing_email = "billing@kleine-buehne.example"
    org.save()
    return org


def _give_german_vat_id(org: Organization) -> Organization:
    """A VIES-validated USt-IdNr., which declaring or keeping the German scheme requires (#1258)."""
    org.vat_id = "DE811234567"
    org.vat_id_validated = True
    org.vat_country_code = "DE"
    org.save()
    return org


@pytest.fixture
def owner_client(organization_owner_user: RevelUser) -> Client:
    refresh = RefreshToken.for_user(organization_owner_user)
    return Client(HTTP_AUTHORIZATION=f"Bearer {str(refresh.access_token)}")  # type: ignore[attr-defined]


def _patch(client: Client, org: Organization, data: dict[str, t.Any]) -> t.Any:
    return client.patch(
        reverse("api:update_billing_info", kwargs={"slug": org.slug}),
        data=orjson.dumps(data),
        content_type="application/json",
    )


# ---------------------------------------------------------------------------
# Pure utility
# ---------------------------------------------------------------------------


class TestSmallBusinessUtils:
    @pytest.mark.parametrize(
        ("country", "number", "valid"),
        [
            ("DE", "12/345/67890", True),  # 11-digit Land format
            ("DE", "1234567890", True),  # 10-digit Land format
            ("DE", "1121081508150", True),  # 13-digit bundeseinheitliches Format
            ("DE", "12/345/6789", False),  # 9 digits
            ("DE", "DE123456789", False),  # a USt-IdNr. is not a Steuernummer
            ("AT", "12-345/6789", True),  # Finanzamt + 7 digits
            ("AT", "123456789", True),
            ("AT", "12 345 678", False),
            ("AT", "", False),
            ("FR", "anything", True),  # no known format: accepted as declared
            ("FR", "   ", False),
        ],
    )
    def test_tax_number_format(self, country: str, number: str, valid: bool) -> None:
        assert small_business.tax_number_is_valid(country, number) is valid

    def test_scheme_tables_agree_with_the_model_enum(self) -> None:
        assert set(small_business.SCHEME_COUNTRIES) == {Scheme.DE_19, Scheme.AT_6_1_27}
        assert small_business.scheme_country(Scheme.DE_19) == "DE"
        assert small_business.scheme_country(Scheme.AT_6_1_27) == "AT"
        assert small_business.scheme_country(Scheme.NONE) == ""
        assert small_business.scheme_exemption_reason(Scheme.DE_19) == DE_REASON
        assert small_business.scheme_exemption_reason(Scheme.AT_6_1_27) == AT_REASON
        assert small_business.scheme_exemption_reason(Scheme.NONE) == ""

    def test_labels(self) -> None:
        assert small_business.tax_number_label("DE") == "Steuernummer"
        assert small_business.tax_number_label("at") == "Steuernummer"
        assert small_business.tax_number_label("") == small_business.GENERIC_TAX_NUMBER_LABEL


# ---------------------------------------------------------------------------
# Prerequisites
# ---------------------------------------------------------------------------


class TestPrerequisites:
    @pytest.mark.parametrize("scheme", [Scheme.DE_19, Scheme.AT_6_1_27])
    def test_a_complete_scheme_identity_turns_invoicing_on(self, organization: Organization, scheme: str) -> None:
        org = _make_small_business(organization, scheme)
        assert resolve_org_country(org) == small_business.scheme_country(scheme)  # DE: VAT country; AT: billing
        validate_invoicing_prerequisites(org)
        set_invoicing_mode(org, Organization.InvoicingMode.AUTO)
        org.refresh_from_db()
        assert org.invoicing_mode == Organization.InvoicingMode.AUTO
        assert einvoice_missing_fields(org) == []

    def test_austrian_scheme_replaces_the_vies_validated_vat_id(self, organization: Organization) -> None:
        org = _make_small_business(organization, Scheme.AT_6_1_27)
        assert org.vat_id == "" and not org.vat_id_validated
        validate_invoicing_prerequisites(org)

    def test_german_scheme_needs_a_validated_vat_id(self, organization: Organization) -> None:
        """#1258: the platform fee to a DE Kleinunternehmer is reverse-charged, so the USt-IdNr. is required."""
        org = _make_small_business(organization)
        org.tax_registration_number = ""  # the Steuernummer is optional once the USt-IdNr. identifies the seller
        org.save()
        validate_invoicing_prerequisites(org)
        assert einvoice_missing_fields(org) == []
        org.vat_id_validated = False  # typed in but not validated, or VIES revalidation flipped it
        org.tax_registration_number = "12/345/67890"
        org.save()
        with pytest.raises(HttpError) as exc_info:
            validate_invoicing_prerequisites(org)
        assert exc_info.value.status_code == 422
        assert "USt-IdNr." in str(exc_info.value) and "BZSt" in str(exc_info.value)
        assert "EU-based" not in str(exc_info.value)  # one clear message, not two
        assert einvoice_missing_fields(org) == ["vat_id"]
        assert small_business.scheme_identity_complete(Scheme.DE_19, False, "DE", "12/345/67890") is False
        assert small_business.scheme_identity_complete(Scheme.DE_19, True, "DE", "") is True

    def test_group_b_is_still_refused(self, organization: Organization) -> None:
        """A tax number without a scheme does not replace the VAT ID (VAT-charging organizers, out of scope)."""
        org = _make_small_business(organization, Scheme.AT_6_1_27)
        org.small_business_scheme = Scheme.NONE
        org.save()
        with pytest.raises(HttpError) as exc_info:
            validate_invoicing_prerequisites(org)
        assert exc_info.value.status_code == 422
        assert "VAT ID must be validated via VIES" in str(exc_info.value)
        assert einvoice_missing_fields(org) == ["vat_id"]

    def test_missing_or_malformed_tax_number(self, organization: Organization) -> None:
        org = _make_small_business(organization, Scheme.AT_6_1_27)
        org.tax_registration_number = "ATU12345678"
        org.save()
        with pytest.raises(HttpError) as exc_info:
            validate_invoicing_prerequisites(org)
        assert exc_info.value.status_code == 422
        assert "tax registration number (Steuernummer)" in str(exc_info.value)
        assert einvoice_missing_fields(org) == ["tax_registration_number"]

    def test_scheme_must_match_the_country_of_establishment(self, organization: Organization) -> None:
        org = _make_small_business(organization, Scheme.AT_6_1_27)
        org.billing_country_code = "DE"  # an Austrian scheme declared by an organizer established in Germany
        org.save()
        with pytest.raises(HttpError) as exc_info:
            validate_invoicing_prerequisites(org)
        assert "established in Austria" in str(exc_info.value)
        assert einvoice_missing_fields(org) == ["small_business_scheme"]

    def test_country_outside_the_allowlist(self, organization: Organization) -> None:
        """Only DE and AT schemes exist, so a French organizer cannot invoice through a scheme."""
        org = _make_small_business(organization, Scheme.AT_6_1_27)
        org.billing_country_code = "FR"
        org.save()
        with pytest.raises(HttpError):
            validate_invoicing_prerequisites(org)

    def test_declared_vat_country_takes_precedence(self, organization: Organization) -> None:
        org = _make_small_business(organization)
        org.vat_country_code = "AT"
        org.save()
        with pytest.raises(HttpError):
            validate_invoicing_prerequisites(org)

    def test_validated_vat_id_keeps_the_vies_path(self, organization: Organization) -> None:
        """A Kleinunternehmer that also holds a USt-IdNr. is checked as before (EU VAT country + VIES)."""
        org = _make_org_invoicing_ready(organization)
        org.small_business_scheme = Scheme.DE_19
        org.vat_country_code = ""
        org.save()
        with pytest.raises(HttpError) as exc_info:
            validate_invoicing_prerequisites(org)
        assert "EU-based" in str(exc_info.value)

    def test_billing_info_endpoint_exposes_the_fields(self, organization: Organization, owner_client: Client) -> None:
        org = _make_small_business(organization)
        response = owner_client.get(reverse("api:get_billing_info", kwargs={"slug": org.slug}))
        data = response.json()
        assert data["small_business_scheme"] == "de_19"
        assert data["tax_registration_number"] == "12/345/67890"
        assert data["einvoice_ready"] is True


# ---------------------------------------------------------------------------
# Billing-info PATCH
# ---------------------------------------------------------------------------


class TestBillingUpdate:
    def test_declaring_a_scheme_prefills_the_statutory_note(
        self, organization: Organization, owner_client: Client
    ) -> None:
        organization.vat_rate = Decimal("0.00")
        organization.billing_country_code = "AT"
        organization.save()
        response = _patch(
            owner_client,
            organization,
            {"small_business_scheme": "at_6_1_27", "tax_registration_number": "12-345/6789"},
        )
        assert response.status_code == 200, response.content
        organization.refresh_from_db()
        assert organization.small_business_scheme == Scheme.AT_6_1_27
        assert organization.tax_registration_number == "12-345/6789"
        assert organization.vat_exemption_reason == AT_REASON

    def test_an_existing_reason_is_kept(self, organization: Organization, owner_client: Client) -> None:
        organization.vat_rate = Decimal("0.00")
        organization.vat_exemption_reason = "Own wording"
        organization.billing_country_code = "DE"
        _give_german_vat_id(organization)
        assert _patch(owner_client, organization, {"small_business_scheme": "de_19"}).status_code == 200
        organization.refresh_from_db()
        assert organization.vat_exemption_reason == "Own wording"

    def test_declaring_the_german_scheme_needs_a_validated_vat_id(
        self, organization: Organization, owner_client: Client
    ) -> None:
        """#1258: whatever the invoicing mode, since Revel's fee to a DE Kleinunternehmer is reverse-charged."""
        organization.vat_rate = Decimal("0.00")
        organization.billing_country_code = "DE"
        organization.save()
        assert organization.invoicing_mode == Organization.InvoicingMode.NONE
        response = _patch(owner_client, organization, {"small_business_scheme": "de_19"})
        assert response.status_code == 422
        assert "USt-IdNr." in response.json()["detail"] and "BZSt" in response.json()["detail"]
        organization.vat_id = "DE811234567"  # typed in, VIES said no (or not yet)
        organization.vat_country_code = "DE"
        organization.save()
        assert _patch(owner_client, organization, {"small_business_scheme": "de_19"}).status_code == 422
        organization.vat_id_validated = True
        organization.save()
        assert _patch(owner_client, organization, {"small_business_scheme": "de_19"}).status_code == 200
        organization.refresh_from_db()
        assert organization.small_business_scheme == Scheme.DE_19

    def test_a_lapsed_german_organizer_cannot_keep_the_scheme_on_a_billing_patch(
        self, organization: Organization, owner_client: Client
    ) -> None:
        """Once the validation lapses, a billing PATCH that keeps de_19 is refused until the USt-IdNr. is validated."""
        org = _make_small_business(organization)
        Organization.objects.filter(pk=org.pk).update(vat_id_validated=False)
        response = _patch(owner_client, org, {"billing_name": "Renamed"})
        assert response.status_code == 422
        assert "USt-IdNr." in response.json()["detail"]
        assert _patch(owner_client, org, {"small_business_scheme": "none"}).status_code == 200  # the way out

    def test_a_scheme_needs_a_zero_default_rate(self, organization: Organization, owner_client: Client) -> None:
        organization.vat_rate = Decimal("19.00")
        organization.billing_country_code = "DE"
        _give_german_vat_id(organization)
        response = _patch(owner_client, organization, {"small_business_scheme": "de_19"})
        assert response.status_code == 422
        assert "default VAT rate to 0%" in response.json()["detail"]
        # Sending the rate alongside is fine.
        response = _patch(owner_client, organization, {"small_business_scheme": "de_19", "vat_rate": "0.00"})
        assert response.status_code == 200, response.content

    def test_a_scheme_refuses_live_tiers_charging_vat(
        self, organization: Organization, event: Event, event_ticket_tier: TicketTier, owner_client: Client
    ) -> None:
        organization.vat_rate = Decimal("0.00")
        organization.billing_country_code = "DE"
        _give_german_vat_id(organization)
        event_ticket_tier.vat_rate = Decimal("19.00")
        event_ticket_tier.save()
        response = _patch(owner_client, organization, {"small_business_scheme": "de_19"})
        assert response.status_code == 422
        assert "ticket tiers still charge VAT" in response.json()["detail"]

    def test_malformed_tax_number_is_refused(self, organization: Organization, owner_client: Client) -> None:
        organization.vat_rate = Decimal("0.00")
        organization.billing_country_code = "DE"
        organization.save()
        response = _patch(
            owner_client, organization, {"small_business_scheme": "de_19", "tax_registration_number": "12/345"}
        )
        assert response.status_code == 422
        assert "Steuernummer" in response.json()["detail"]

    def test_tax_number_alone_is_checked_against_the_billing_country(
        self, organization: Organization, owner_client: Client
    ) -> None:
        organization.billing_country_code = "AT"
        organization.save()
        assert _patch(owner_client, organization, {"tax_registration_number": "12/345/67890"}).status_code == 422
        assert _patch(owner_client, organization, {"tax_registration_number": "12-345/6789"}).status_code == 200

    def test_tax_number_without_a_known_format_is_stored_as_declared(
        self, organization: Organization, owner_client: Client
    ) -> None:
        assert _patch(owner_client, organization, {"tax_registration_number": "FR-whatever"}).status_code == 200

    def test_prepare_leaves_unrelated_updates_alone(self, organization: Organization) -> None:
        data = {"billing_name": "X"}
        assert service.prepare_billing_update(organization, data) is data

    def test_tier_with_vat_is_refused_under_a_scheme(self, organization: Organization, event: Event) -> None:
        org = _make_small_business(organization)
        tier = TicketTier(event=event, name="T", price=Decimal("10.00"), vat_rate=Decimal("19.00"))
        with pytest.raises(HttpError) as exc_info:
            _assert_zero_rate_has_exemption_reason(tier, org)
        assert exc_info.value.status_code == 422
        tier.vat_rate = Decimal("0.00")
        _assert_zero_rate_has_exemption_reason(tier, org)  # the org default reason covers it


# ---------------------------------------------------------------------------
# Checkout VAT
# ---------------------------------------------------------------------------


class TestCheckoutVat:
    @pytest.mark.parametrize(
        ("buyer_country", "buyer_vat_valid", "is_virtual"),
        [("DE", False, False), ("FR", True, True), ("US", False, True), ("FR", False, True)],
    )
    def test_small_business_seller_never_charges_or_reverse_charges(
        self, buyer_country: str, buyer_vat_valid: bool, is_virtual: bool
    ) -> None:
        result = determine_attendee_vat(
            gross_price=Decimal("50.00"),
            seller_vat_rate=Decimal("19.00"),  # a stray rate must not leak into the price
            seller_country="DE",
            buyer_country=buyer_country,
            buyer_vat_id_valid=buyer_vat_valid,
            is_virtual=is_virtual,
            seller_small_business=True,
        )
        assert result.effective_price == result.net_amount == Decimal("50.00")
        assert result.vat_amount == result.vat_rate == 0
        assert result.reverse_charge is False

    def test_default_is_unchanged(self) -> None:
        result = determine_attendee_vat(Decimal("50.00"), Decimal("19.00"), "DE", "FR", True, is_virtual=True)
        assert result.reverse_charge is True


# ---------------------------------------------------------------------------
# Generation
# ---------------------------------------------------------------------------


def _ready(organization: Organization, event: Event, *, virtual: bool = False, scheme: str = Scheme.DE_19) -> None:
    org = _make_small_business(organization, scheme)
    org.invoicing_mode = Organization.InvoicingMode.AUTO
    org.save()
    event.is_virtual = virtual
    event.save()


def _payment(user: RevelUser, event: Event, tier: TicketTier, session: str, snapshot: dict[str, t.Any]) -> t.Any:
    return _create_payment(
        user=user,
        event=event,
        tier=tier,
        session_id=session,
        buyer_billing_snapshot=snapshot,
        net_amount=Decimal("100.00"),
        vat_amount=Decimal("0.00"),
        vat_rate=Decimal("0.00"),
    )


@patch(MOCK_RENDER_PDF, return_value=b"fake-pdf")
@patch(MOCK_SEND_EMAIL)
class TestGeneration:
    def test_every_line_is_exempt_with_the_statutory_note(
        self,
        _mail: t.Any,
        _pdf: t.Any,
        organization: Organization,
        event: Event,
        event_ticket_tier: TicketTier,
        member_user: RevelUser,
    ) -> None:
        _ready(organization, event)
        _payment(member_user, event, event_ticket_tier, "cs_sb_1", _default_billing_snapshot())
        invoice = generate_attendee_invoice("cs_sb_1")
        assert invoice is not None
        assert [li["vat_category"] for li in invoice.line_items] == ["E"]
        assert invoice.vat_exemptions["E"]["text"] == DE_REASON
        assert invoice.total_vat == 0 and invoice.total_net == invoice.total_gross == Decimal("100.00")
        assert invoice.seller_vat_id == "DE811234567"  # BT-31 (#1258); the Steuernummer stays as BT-32
        assert invoice.seller_small_business_scheme == Scheme.DE_19  # the PDF then shows no VAT at all
        assert invoice.seller_tax_registration_number == "12/345/67890"
        assert invoice.seller_country_code == "DE"
        assert invoice.reverse_charge is False

    def test_a_reverse_charge_snapshot_is_ignored(
        self,
        _mail: t.Any,
        _pdf: t.Any,
        organization: Organization,
        event: Event,
        event_ticket_tier: TicketTier,
        member_user: RevelUser,
    ) -> None:
        """Defense in depth: checkout never sets it for a scheme seller; a stale snapshot still yields E."""
        _ready(organization, event, virtual=True)
        _payment(member_user, event, event_ticket_tier, "cs_sb_rc", {**_default_billing_snapshot(True)})
        invoice = generate_attendee_invoice("cs_sb_rc")
        assert invoice is not None
        assert invoice.reverse_charge is False
        assert [li["vat_category"] for li in invoice.line_items] == ["E"]

    def test_virtual_sale_to_non_eu_buyer_is_exempt_not_out_of_scope(
        self,
        _mail: t.Any,
        _pdf: t.Any,
        organization: Organization,
        event: Event,
        event_ticket_tier: TicketTier,
        member_user: RevelUser,
    ) -> None:
        """The place of supply moves to the customer: outside the scheme, so not invoiced (see the review tests)."""
        _ready(organization, event, virtual=True)
        snapshot = {**_default_billing_snapshot(), "vat_id": "", "vat_country_code": "US"}
        _payment(member_user, event, event_ticket_tier, "cs_sb_us", snapshot)
        assert generate_attendee_invoice("cs_sb_us") is None
        skipped = SkippedFiscalDocument.objects.get(stripe_session_id="cs_sb_us")
        assert skipped.reason_code == SkippedFiscalDocument.ReasonCode.SMALL_BUSINESS_VIRTUAL_CROSS_BORDER
        assert [li["vat_category"] for li in skipped.line_items] == ["E"]

    def test_virtual_sale_to_a_business_in_another_eu_country_is_skipped(
        self,
        _mail: t.Any,
        _pdf: t.Any,
        organization: Organization,
        event: Event,
        event_ticket_tier: TicketTier,
        member_user: RevelUser,
    ) -> None:
        _ready(organization, event, virtual=True)
        snapshot = {
            **_default_billing_snapshot(),
            "vat_id": "FR12345678901",
            "vat_country_code": "FR",
            "vat_id_status": "valid",
        }
        _payment(member_user, event, event_ticket_tier, "cs_sb_fr", snapshot)
        assert generate_attendee_invoice("cs_sb_fr") is None
        skipped = SkippedFiscalDocument.objects.get(stripe_session_id="cs_sb_fr")
        assert skipped.reason_code == SkippedFiscalDocument.ReasonCode.SMALL_BUSINESS_VIRTUAL_CROSS_BORDER
        assert skipped.policy_country == "DE"
        assert "France" in skipped.reason
        assert [li["vat_category"] for li in skipped.line_items] == ["E"]

    @pytest.mark.parametrize(
        ("virtual", "vat_id", "country"),
        [
            (False, "FR12345678901", "FR"),  # physical admission at home: taxed at the venue
            (True, "DE123456789", "DE"),  # domestic business buyer
            (True, "", "DE"),  # domestic consumer
        ],
    )
    def test_domestic_buyers_are_invoiced(
        self,
        _mail: t.Any,
        _pdf: t.Any,
        organization: Organization,
        event: Event,
        event_ticket_tier: TicketTier,
        member_user: RevelUser,
        virtual: bool,
        vat_id: str,
        country: str,
    ) -> None:
        _ready(organization, event, virtual=virtual)
        snapshot = {**_default_billing_snapshot(), "vat_id": vat_id, "vat_country_code": country}
        _payment(member_user, event, event_ticket_tier, "cs_sb_ok", snapshot)
        assert generate_attendee_invoice("cs_sb_ok") is not None

    def test_gate_is_a_no_op_for_other_organizers(
        self, _mail: t.Any, _pdf: t.Any, organization: Organization, event: Event
    ) -> None:
        event.is_virtual = True
        assert service.sale_decision(organization, event, "FR").allowed
        assert service.seller_identity_decision(_make_org_invoicing_ready(organization)).allowed


# ---------------------------------------------------------------------------
# PDF and drafts
# ---------------------------------------------------------------------------


class TestPdfAndDrafts:
    def test_pdf_prints_the_tax_number_and_the_note(
        self, organization: Organization, event: Event, member_user: RevelUser
    ) -> None:
        _make_small_business(organization, Scheme.AT_6_1_27)
        invoice = _create_draft(organization, event, member_user)
        invoice.seller_vat_id = ""
        invoice.seller_tax_registration_number = "12-345/6789"
        invoice.seller_country_code = "AT"
        invoice.vat_exemptions = {"E": {"text": AT_REASON, "code": ""}}
        invoice.save()
        html = render_to_string(
            "invoices/attendee_invoice.html",
            {"invoice": invoice, "is_draft": False, "is_export": False, "is_virtual_interim": False},
        )
        assert "Steuernummer: 12-345/6789" in html
        assert "VAT: " not in html.split("Bill To")[0]
        assert "VAT exempt: Umsatzsteuerbefreit – Kleinunternehmer gem. § 6 Abs. 1 Z 27 UStG" in html

    def test_german_pdf_prints_the_vat_id_and_the_note(
        self, organization: Organization, event: Event, member_user: RevelUser
    ) -> None:
        """#1258: § 34a S. 1 Nr. 2 UStDV accepts the USt-IdNr. in place of the Steuernummer; Nr. 5 wants the note."""
        _make_small_business(organization)
        invoice = _create_draft(organization, event, member_user)
        invoice.seller_vat_id = "DE811234567"
        invoice.seller_tax_registration_number = "12/345/67890"
        invoice.seller_country_code = "DE"
        invoice.vat_exemptions = {"E": {"text": DE_REASON, "code": ""}}
        invoice.save()
        html = render_to_string(
            "invoices/attendee_invoice.html",
            {"invoice": invoice, "is_draft": False, "is_export": False, "is_virtual_interim": False},
        )
        assert "VAT: DE811234567" in html
        assert "VAT exempt: Steuerbefreit gemäß § 19 UStG" in html

    def test_vat_id_still_wins_on_the_pdf(
        self, organization: Organization, event: Event, member_user: RevelUser
    ) -> None:
        invoice = _create_draft(organization, event, member_user)
        invoice.seller_vat_id = "DE811234567"
        invoice.seller_tax_registration_number = "12/345/67890"
        invoice.save()
        html = render_to_string(
            "invoices/attendee_invoice.html",
            {"invoice": invoice, "is_draft": False, "is_export": False, "is_virtual_interim": False},
        )
        assert "VAT: DE811234567" in html
        assert "Steuernummer" not in html

    def test_draft_edit_cannot_switch_to_reverse_charge(
        self, organization: Organization, event: Event, member_user: RevelUser
    ) -> None:
        _make_small_business(organization)
        invoice = _create_draft(organization, event, member_user)
        with pytest.raises(HttpError) as exc_info:
            update_draft_invoice(invoice, {"reverse_charge": True})
        assert exc_info.value.status_code == 422

    def test_draft_edit_keeps_lines_exempt_for_a_non_eu_buyer_of_a_virtual_event(
        self, organization: Organization, event: Event, member_user: RevelUser
    ) -> None:
        _make_small_business(organization)
        event.is_virtual = True
        event.save()
        invoice = _create_draft(organization, event, member_user)
        invoice.seller_vat_country = "DE"
        invoice.line_items = [{**invoice.line_items[0], "vat_rate": "0.00", "vat_amount": "0.00", "vat_category": "E"}]
        invoice.save()
        updated = update_draft_invoice(invoice, {"buyer_vat_country": "US"})
        updated.refresh_from_db()
        assert [li["vat_category"] for li in updated.line_items] == ["E"]


# ---------------------------------------------------------------------------
# Platform fee (Revel → organizer) and rolling-deploy safety
# ---------------------------------------------------------------------------


def test_platform_fee_to_a_german_scheme_organizer_is_reverse_charged(organization: Organization) -> None:
    """#1258: a Kleinunternehmer is a taxable person (§ 3a Abs. 5 Z 1 UStG 1994, UStR Rz 638o), so Revel's fee to a
    DE one is supplied in Germany with reverse charge (Art. 44/196), which is why the scheme requires the USt-IdNr.

    Pinned on purpose (docs/compliance/eu/de.md): the fee follows the existing VIES path, no AT VAT.
    """
    org = _make_small_business(organization)
    reverse_charge, rate = b2b_vat_context(org, "AT", Decimal("20.00"), unknown_country_domestic=True)
    assert (reverse_charge, rate) == (True, Decimal("0.00"))


def test_platform_fee_to_an_austrian_scheme_organizer_stays_domestic(organization: Organization) -> None:
    """An AT Kleinunternehmer buys a domestic supply: Austrian VAT at 20%, whether or not it holds a UID (at.md)."""
    org = _make_small_business(organization, Scheme.AT_6_1_27)
    org.vat_country_code = "AT"
    org.save()
    reverse_charge, rate = b2b_vat_context(org, "AT", Decimal("20.00"), unknown_country_domestic=True)
    assert (reverse_charge, rate) == (False, Decimal("20.00"))


@pytest.mark.parametrize(
    ("table", "column", "expected"),
    [
        ("events_organization", "tax_registration_number", "''::character varying"),
        ("events_organization", "small_business_scheme", "'none'::character varying"),
        ("events_attendeeinvoice", "seller_tax_registration_number", "''::character varying"),
        ("events_attendeeinvoice", "issuer_name", "''::character varying"),
        ("events_attendeeinvoice", "seller_small_business_scheme", "'none'::character varying"),
        ("events_attendeeinvoicecreditnote", "issuer_name", "''::character varying"),
    ],
)
def test_new_not_null_columns_have_a_database_default(table: str, column: str, expected: str) -> None:
    """A pod still on the previous release inserts rows without these columns during the rollout."""
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT column_default, is_nullable FROM information_schema.columns "
            "WHERE table_name = %s AND column_name = %s",
            [table, column],
        )
        default, nullable = cursor.fetchone()
    assert nullable == "NO"
    assert default == expected
