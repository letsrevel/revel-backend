"""Review follow-ups for small-business-scheme invoicing (#1244).

Guards that close the holes a scheme organizer could still fall through: VAT on an edited draft
line, a scheme declared for the wrong country, dropping the scheme while invoicing is on, sales
outside the scheme's country (venue abroad, cross-border virtual), the single zero-VAT choke point,
the compliance decision for drafts, the seller snapshot and the exemption-note bookkeeping.
"""

import typing as t
from decimal import Decimal
from unittest.mock import patch

import orjson
import pytest
from django.test import Client
from django.urls import reverse
from ninja.errors import HttpError
from ninja_jwt.tokens import RefreshToken

from accounts.models import RevelUser
from events.compliance import AttendeeInvoicingCapability
from events.compliance.enforcement import event_compliance
from events.models import Event, Organization, SkippedFiscalDocument, TicketTier
from events.service import attendee_vat_service, vat_service
from events.service.attendee_invoice_draft_service import update_draft_invoice
from events.service.attendee_invoice_lines import _build_line_items, _exempt_reasons
from events.service.attendee_invoice_service import (
    generate_attendee_credit_note,
    generate_attendee_invoice,
    invoice_compliance_decision,
    set_invoicing_mode,
    validate_invoicing_prerequisites,
)
from events.service.stripe_service import _resolve_ticket_amounts
from events.tests.test_attendee_invoice._helpers import (
    MOCK_RENDER_PDF,
    MOCK_SEND_EMAIL,
    _create_draft,
    _create_payment,
    _default_billing_snapshot,
    _make_org_invoicing_ready,
)
from events.tests.test_attendee_invoice.test_small_business_scheme import (
    AT_REASON,
    DE_REASON,
    Scheme,
    _make_small_business,
)

pytestmark = pytest.mark.django_db

Reason = SkippedFiscalDocument.ReasonCode


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


def _invoicing_small_business(organization: Organization, scheme: str = Scheme.DE_19) -> Organization:
    org = _make_small_business(organization, scheme)
    set_invoicing_mode(org, Organization.InvoicingMode.AUTO)
    return org


def _zero_payment(user: RevelUser, event: Event, tier: TicketTier, session: str, snapshot: dict[str, t.Any]) -> t.Any:
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


def _buyer(country: str, vat_id: str = "", status: str = "") -> dict[str, t.Any]:
    return {**_default_billing_snapshot(), "vat_id": vat_id, "vat_country_code": country, "vat_id_status": status}


# ---------------------------------------------------------------------------
# H1: a draft line cannot be edited to charge VAT
# ---------------------------------------------------------------------------


class TestDraftLinesStayExempt:
    def test_line_with_vat_is_refused(self, organization: Organization, event: Event, member_user: RevelUser) -> None:
        _make_small_business(organization)
        invoice = _create_draft(organization, event, member_user)
        line = {**invoice.line_items[0], "vat_rate": Decimal("19.00"), "vat_amount": Decimal("15.97")}
        with pytest.raises(HttpError) as exc_info:
            update_draft_invoice(invoice, {"line_items": [line]})
        assert exc_info.value.status_code == 422
        assert "0" in str(exc_info.value)

    def test_zero_line_stays_exempt(self, organization: Organization, event: Event, member_user: RevelUser) -> None:
        _make_small_business(organization)
        invoice = _create_draft(organization, event, member_user)
        line = {
            **invoice.line_items[0],
            "vat_rate": Decimal("0.00"),
            "vat_amount": Decimal("0.00"),
            "net_amount": Decimal("100.00"),
        }
        updated = update_draft_invoice(invoice, {"line_items": [line]})
        updated.refresh_from_db()
        assert [li["vat_category"] for li in updated.line_items] == ["E"]
        assert DE_REASON in updated.vat_exemptions["E"]["text"]


# ---------------------------------------------------------------------------
# H2 / H3: scheme ↔ country, and no way out once invoicing is on
# ---------------------------------------------------------------------------


class TestSchemeConsistency:
    def test_validated_vat_id_cannot_declare_a_foreign_scheme(
        self, organization: Organization, owner_client: Client
    ) -> None:
        org = _make_org_invoicing_ready(organization)  # validated IT VAT ID
        org.vat_rate = Decimal("0.00")
        org.save()
        response = _patch(owner_client, org, {"small_business_scheme": "de_19"})
        assert response.status_code == 422
        assert "established in Germany" in response.json()["detail"]

    def test_scheme_must_match_the_billing_country_in_the_same_patch(
        self, organization: Organization, owner_client: Client
    ) -> None:
        organization.vat_rate = Decimal("0.00")
        organization.save()
        assert _patch(owner_client, organization, {"small_business_scheme": "at_6_1_27"}).status_code == 422
        response = _patch(
            owner_client, organization, {"small_business_scheme": "at_6_1_27", "billing_country_code": "AT"}
        )
        assert response.status_code == 200, response.content

    def test_prerequisites_check_the_scheme_country_on_the_vies_path(self, organization: Organization) -> None:
        org = _make_org_invoicing_ready(organization)
        org.small_business_scheme = Scheme.DE_19
        org.vat_rate = Decimal("0.00")
        org.save()
        with pytest.raises(HttpError) as exc_info:
            validate_invoicing_prerequisites(org)
        assert "established in Germany" in str(exc_info.value)

    @pytest.mark.parametrize(
        ("data", "fragment"),
        [
            ({"small_business_scheme": "none", "vat_rate": "19.00"}, "VAT ID must be validated"),
            ({"tax_registration_number": ""}, "tax registration number"),
            ({"billing_country_code": "AT"}, "established in Germany"),
            ({"small_business_scheme": "at_6_1_27"}, "established in Austria"),
        ],
    )
    def test_updates_that_would_break_the_seller_identity_are_refused_while_invoicing(
        self, organization: Organization, owner_client: Client, data: dict[str, t.Any], fragment: str
    ) -> None:
        org = _invoicing_small_business(organization)
        response = _patch(owner_client, org, data)
        assert response.status_code == 422, response.content
        assert fragment in response.json()["detail"]
        org.refresh_from_db()
        assert org.small_business_scheme == Scheme.DE_19
        assert org.tax_registration_number == "12/345/67890"

    def test_same_updates_are_fine_with_invoicing_off(self, organization: Organization, owner_client: Client) -> None:
        org = _make_small_business(organization)
        assert _patch(owner_client, org, {"tax_registration_number": ""}).status_code == 200
        assert _patch(owner_client, org, {"small_business_scheme": "none", "vat_rate": "19.00"}).status_code == 200

    @patch(MOCK_RENDER_PDF, return_value=b"fake-pdf")
    @patch(MOCK_SEND_EMAIL)
    def test_generation_skips_when_the_seller_identity_is_missing(
        self,
        _mail: t.Any,
        _pdf: t.Any,
        organization: Organization,
        event: Event,
        event_ticket_tier: TicketTier,
        member_user: RevelUser,
    ) -> None:
        """Defense in depth: a row edited behind the API's back never yields a VAT invoice with no identity."""
        org = _invoicing_small_business(organization)
        Organization.objects.filter(pk=org.pk).update(small_business_scheme=Scheme.NONE, vat_rate=Decimal("19.00"))
        _create_payment(
            user=member_user,
            event=event,
            tier=event_ticket_tier,
            session_id="cs_noid",
            buyer_billing_snapshot=_default_billing_snapshot(),
        )
        assert generate_attendee_invoice("cs_noid") is None
        skipped = SkippedFiscalDocument.objects.get(stripe_session_id="cs_noid")
        assert skipped.reason_code == Reason.SELLER_IDENTITY_MISSING
        assert skipped.policy_country == "DE"


# ---------------------------------------------------------------------------
# M1: sales outside the scheme's country are not invoiced
# ---------------------------------------------------------------------------


@patch(MOCK_RENDER_PDF, return_value=b"fake-pdf")
@patch(MOCK_SEND_EMAIL)
class TestForeignSales:
    def _sell(self, event: Event, tier: TicketTier, user: RevelUser, session: str, snapshot: dict[str, t.Any]) -> t.Any:
        _zero_payment(user, event, tier, session, snapshot)
        return generate_attendee_invoice(session)

    def test_physical_event_abroad_is_skipped(
        self,
        _mail: t.Any,
        _pdf: t.Any,
        organization: Organization,
        event: Event,
        event_ticket_tier: TicketTier,
        member_user: RevelUser,
    ) -> None:
        _invoicing_small_business(organization)
        event.vat_country_code = "AT"
        event.save()
        assert self._sell(event, event_ticket_tier, member_user, "cs_venue", _buyer("DE")) is None
        skipped = SkippedFiscalDocument.objects.get(stripe_session_id="cs_venue")
        assert skipped.reason_code == Reason.SMALL_BUSINESS_FOREIGN_VENUE
        assert skipped.policy_country == "DE"
        assert "Austria" in skipped.reason

    def test_physical_event_at_home_is_invoiced(
        self,
        _mail: t.Any,
        _pdf: t.Any,
        organization: Organization,
        event: Event,
        event_ticket_tier: TicketTier,
        member_user: RevelUser,
    ) -> None:
        _invoicing_small_business(organization)
        event.vat_country_code = "DE"
        event.save()
        assert (
            self._sell(event, event_ticket_tier, member_user, "cs_home", _buyer("FR", "FR12345678901", "valid"))
            is not None
        )

    @pytest.mark.parametrize(
        ("country", "vat_id", "status"),
        [("FR", "FR12345678901", "valid"), ("FR", "", ""), ("US", "", ""), ("US", "US12345", "")],
    )
    def test_virtual_event_sold_abroad_is_skipped(
        self,
        _mail: t.Any,
        _pdf: t.Any,
        organization: Organization,
        event: Event,
        event_ticket_tier: TicketTier,
        member_user: RevelUser,
        country: str,
        vat_id: str,
        status: str,
    ) -> None:
        _invoicing_small_business(organization)
        event.is_virtual = True
        event.save()
        assert self._sell(event, event_ticket_tier, member_user, "cs_vx", _buyer(country, vat_id, status)) is None
        skipped = SkippedFiscalDocument.objects.get(stripe_session_id="cs_vx")
        assert skipped.reason_code == Reason.SMALL_BUSINESS_VIRTUAL_CROSS_BORDER
        assert skipped.policy_country == "DE"

    @pytest.mark.parametrize("country", ["DE", ""])
    def test_virtual_event_sold_at_home_is_invoiced(
        self,
        _mail: t.Any,
        _pdf: t.Any,
        organization: Organization,
        event: Event,
        event_ticket_tier: TicketTier,
        member_user: RevelUser,
        country: str,
    ) -> None:
        _invoicing_small_business(organization)
        event.is_virtual = True
        event.save()
        invoice = self._sell(
            event, event_ticket_tier, member_user, "cs_vh", _buyer(country, "DE123456789" if country else "")
        )
        assert invoice is not None
        assert invoice.seller_vat_country == "DE"  # L4: resolved from the billing address, not the blank VAT country

    def test_austrian_scheme_follows_its_own_country(
        self,
        _mail: t.Any,
        _pdf: t.Any,
        organization: Organization,
        event: Event,
        event_ticket_tier: TicketTier,
        member_user: RevelUser,
    ) -> None:
        _invoicing_small_business(organization, Scheme.AT_6_1_27)
        event.vat_country_code = "AT"
        event.save()
        invoice = self._sell(event, event_ticket_tier, member_user, "cs_at", _buyer("AT"))
        assert invoice is not None
        assert invoice.vat_exemptions["E"]["text"] == AT_REASON

    def test_other_organizers_are_untouched(
        self,
        _mail: t.Any,
        _pdf: t.Any,
        organization: Organization,
        event: Event,
        event_ticket_tier: TicketTier,
        member_user: RevelUser,
    ) -> None:
        org = _make_org_invoicing_ready(organization)
        org.invoicing_mode = Organization.InvoicingMode.AUTO
        org.save()
        event.vat_country_code = "AT"
        event.save()
        assert self._sell(event, event_ticket_tier, member_user, "cs_it", _buyer("AT")) is not None


class TestEventCompliance:
    def test_physical_event_abroad_is_blocked(self, organization: Organization, event: Event) -> None:
        _make_small_business(organization)
        event.vat_country_code = "AT"
        assert event_compliance(event).attendee_invoicing == AttendeeInvoicingCapability.BLOCKED

    def test_physical_event_at_home_is_allowed(self, organization: Organization, event: Event) -> None:
        _make_small_business(organization)
        event.vat_country_code = "DE"
        assert event_compliance(event).attendee_invoicing == AttendeeInvoicingCapability.ALLOWED

    def test_virtual_event_is_blocked_for_foreign_buyers(self, organization: Organization, event: Event) -> None:
        _make_small_business(organization)
        event.is_virtual = True
        assert event_compliance(event).attendee_invoicing == AttendeeInvoicingCapability.BLOCKED_FOR_FOREIGN_BUYERS

    def test_other_organizers_are_untouched(self, organization: Organization, event: Event) -> None:
        _make_org_invoicing_ready(organization)
        event.vat_country_code = "AT"
        assert event_compliance(event).attendee_invoicing == AttendeeInvoicingCapability.ALLOWED


# ---------------------------------------------------------------------------
# M2: one zero-VAT choke point
# ---------------------------------------------------------------------------


class TestZeroVatChokePoint:
    def test_both_effective_rate_variants_return_zero(self, organization: Organization, event: Event) -> None:
        org = _make_small_business(organization)
        tier = TicketTier(event=event, name="T", price=Decimal("10.00"), vat_rate=Decimal("19.00"))
        assert attendee_vat_service.get_effective_vat_rate(tier, org) == 0
        assert vat_service.get_effective_vat_rate(tier.vat_rate, org.vat_rate, small_business=True) == 0
        assert vat_service.get_effective_vat_rate(tier.vat_rate, org.vat_rate) == Decimal("19.00")

    def test_checkout_without_buyer_country_on_a_duplicated_vat_tier(
        self, organization: Organization, event: Event
    ) -> None:
        """A tier copied with its 19% rate (event duplication bypasses the tier guard) still prices at 0%."""
        org = _make_small_business(organization)
        tier = TicketTier.objects.create(event=event, name="Copy", price=Decimal("50.00"), vat_rate=Decimal("19.00"))
        amounts, _ = _resolve_ticket_amounts([(Decimal("50.00"), tier)], org=org, buyer_vat_context=None)
        assert (amounts[0].net_amount, amounts[0].vat_amount, amounts[0].vat_rate) == (
            Decimal("50.00"),
            Decimal("0.00"),
            Decimal("0.00"),
        )

    def test_line_builder_forces_exempt_lines(
        self, organization: Organization, event: Event, event_ticket_tier: TicketTier, member_user: RevelUser
    ) -> None:
        _make_small_business(organization)
        payment = _create_payment(user=member_user, event=event, tier=event_ticket_tier, session_id="cs_l")
        [item] = _build_line_items([payment], small_business=True)
        assert (item["net_amount"], item["vat_amount"], item["vat_rate"], item["vat_category"]) == (
            "100.00",
            "0.00",
            "0.00",
            "E",
        )
        [item] = _build_line_items([payment])
        assert item["vat_category"] == "S"

    @patch(MOCK_RENDER_PDF, return_value=b"fake-pdf")
    @patch(MOCK_SEND_EMAIL)
    def test_credit_note_lines_are_exempt_too(
        self,
        _mail: t.Any,
        _pdf: t.Any,
        organization: Organization,
        event: Event,
        event_ticket_tier: TicketTier,
        member_user: RevelUser,
    ) -> None:
        """A sale recorded with VAT before the scheme was declared is credited without any."""
        org = _make_org_invoicing_ready(organization)
        org.invoicing_mode = Organization.InvoicingMode.AUTO
        org.save()
        event.vat_country_code = "DE"
        event.save()
        payment = _create_payment(
            user=member_user,
            event=event,
            tier=event_ticket_tier,
            session_id="cs_pre",
            buyer_billing_snapshot=_default_billing_snapshot(),
        )
        assert generate_attendee_invoice("cs_pre") is not None
        _make_small_business(org)
        org.invoicing_mode = Organization.InvoicingMode.AUTO
        org.save()
        note = generate_attendee_credit_note("cs_pre", [payment.id])
        assert note is not None
        assert [li["vat_category"] for li in note.line_items] == ["E"]
        assert note.amount_vat == 0


# ---------------------------------------------------------------------------
# M3: the compliance decision for drafts and credit notes
# ---------------------------------------------------------------------------


class TestInvoiceComplianceDecision:
    def test_virtual_draft_for_a_foreign_business_buyer_is_refused(
        self, organization: Organization, event: Event, member_user: RevelUser
    ) -> None:
        _make_small_business(organization)
        event.is_virtual = True
        event.save()
        invoice = _create_draft(organization, event, member_user)
        invoice.buyer_vat_id = "FR12345678901"
        invoice.buyer_vat_country = "FR"
        invoice.buyer_country_code = "FR"
        invoice.save()
        decision = invoice_compliance_decision(invoice)
        assert not decision.allowed
        assert decision.code == Reason.SMALL_BUSINESS_VIRTUAL_CROSS_BORDER

    def test_physical_draft_abroad_is_refused(
        self, organization: Organization, event: Event, member_user: RevelUser
    ) -> None:
        _make_small_business(organization)
        event.vat_country_code = "AT"
        event.save()
        invoice = _create_draft(organization, event, member_user)
        assert invoice_compliance_decision(invoice).code == Reason.SMALL_BUSINESS_FOREIGN_VENUE

    def test_domestic_draft_is_allowed(self, organization: Organization, event: Event, member_user: RevelUser) -> None:
        _make_small_business(organization)
        event.vat_country_code = "DE"
        event.save()
        assert invoice_compliance_decision(_create_draft(organization, event, member_user)).allowed


# ---------------------------------------------------------------------------
# L1 / L2: seller snapshot and the exemption note
# ---------------------------------------------------------------------------


class TestSnapshotAndNote:
    @patch(MOCK_RENDER_PDF, return_value=b"fake-pdf")
    @patch(MOCK_SEND_EMAIL)
    def test_unvalidated_vat_id_is_not_copied_to_the_invoice(
        self,
        _mail: t.Any,
        _pdf: t.Any,
        organization: Organization,
        event: Event,
        event_ticket_tier: TicketTier,
        member_user: RevelUser,
    ) -> None:
        org = _invoicing_small_business(organization)
        org.vat_id = "DE811234567"  # typed in, never VIES-validated
        org.save()
        event.vat_country_code = "DE"
        event.save()
        _zero_payment(member_user, event, event_ticket_tier, "cs_unval", _buyer("DE"))
        invoice = generate_attendee_invoice("cs_unval")
        assert invoice is not None
        assert invoice.seller_vat_id == ""
        assert invoice.seller_tax_registration_number == "12/345/67890"

    def test_validated_vat_id_is_still_copied(
        self, organization: Organization, event: Event, event_ticket_tier: TicketTier, member_user: RevelUser
    ) -> None:
        org = _make_org_invoicing_ready(organization)
        org.invoicing_mode = Organization.InvoicingMode.AUTO
        org.save()
        with patch(MOCK_RENDER_PDF, return_value=b"pdf"), patch(MOCK_SEND_EMAIL):
            _create_payment(
                user=member_user,
                event=event,
                tier=event_ticket_tier,
                session_id="cs_val",
                buyer_billing_snapshot=_default_billing_snapshot(),
            )
            invoice = generate_attendee_invoice("cs_val")
        assert invoice is not None
        assert invoice.seller_vat_id == "IT12345678901"

    def test_switching_scheme_replaces_the_prefilled_note(
        self, organization: Organization, owner_client: Client
    ) -> None:
        org = _make_small_business(organization)
        assert org.vat_exemption_reason == DE_REASON
        response = _patch(
            owner_client,
            org,
            {
                "small_business_scheme": "at_6_1_27",
                "billing_country_code": "AT",
                "tax_registration_number": "12-345/6789",
            },
        )
        assert response.status_code == 200, response.content
        org.refresh_from_db()
        assert org.vat_exemption_reason == AT_REASON

    def test_dropping_the_scheme_clears_the_prefilled_note(
        self, organization: Organization, owner_client: Client
    ) -> None:
        org = _make_small_business(organization)
        assert _patch(owner_client, org, {"small_business_scheme": "none"}).status_code == 200
        org.refresh_from_db()
        assert org.vat_exemption_reason == ""

    def test_own_wording_is_kept_and_the_note_still_reaches_the_invoice(
        self,
        organization: Organization,
        owner_client: Client,
        event: Event,
        event_ticket_tier: TicketTier,
        member_user: RevelUser,
    ) -> None:
        organization.vat_rate = Decimal("0.00")
        organization.vat_exemption_reason = "Own wording"
        organization.billing_country_code = "DE"
        organization.save()
        assert _patch(owner_client, organization, {"small_business_scheme": "de_19"}).status_code == 200
        organization.refresh_from_db()
        assert organization.vat_exemption_reason == "Own wording"
        payment = _zero_payment(member_user, event, event_ticket_tier, "cs_own", _buyer("DE"))
        reasons = _exempt_reasons([payment], _build_line_items([payment], small_business=True), organization)
        assert reasons[0] == DE_REASON
        assert "Own wording" in reasons

    def test_tax_number_is_checked_against_the_new_country(
        self, organization: Organization, owner_client: Client
    ) -> None:
        organization.billing_country_code = "DE"
        organization.save()
        response = _patch(
            owner_client, organization, {"billing_country_code": "AT", "tax_registration_number": "12/345/67890"}
        )
        assert response.status_code == 422
        assert "Austria" in response.json()["detail"]
