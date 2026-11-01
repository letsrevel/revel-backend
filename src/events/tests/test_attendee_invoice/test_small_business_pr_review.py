"""PR #1257 code-review follow-ups for small-business-scheme invoicing (#1244)."""

import datetime
import typing as t
from decimal import Decimal
from unittest.mock import patch

import pytest
from django.core.cache import cache
from django.template.loader import render_to_string
from django.test import Client
from django.urls import reverse
from django.utils import timezone
from ninja.errors import HttpError
from ninja_jwt.tokens import RefreshToken

from accounts.models import RevelUser
from common.service.vies_service import VIESUnavailableError, VIESValidationResult
from events.compliance import AttendeeInvoicingCapability
from events.compliance.enforcement import event_compliance
from events.exceptions import CountryComplianceError
from events.models import Event, Organization, TicketTier
from events.models.attendee_invoice import AttendeeInvoice
from events.service import einvoice_mapping as mapping
from events.service import small_business_scheme_service as service
from events.service.attendee_invoice_draft_service import (
    _follow_organizer_scheme,
    issue_draft_invoice,
    update_draft_invoice,
)
from events.service.attendee_invoice_service import generate_attendee_credit_note, generate_attendee_invoice
from events.service.revenue_aggregation import ReportScope, build_revenue_report_data
from events.tests.test_attendee_invoice._helpers import (
    MOCK_RENDER_PDF,
    MOCK_SEND_EMAIL,
    _create_draft,
    _create_payment,
    _default_billing_snapshot,
    _make_org_invoicing_ready,
)
from events.tests.test_attendee_invoice.test_small_business_scheme import _make_small_business
from events.tests.test_einvoice._fixtures import line, make_invoice
from events.utils.small_business import tax_number_is_valid

pytestmark = pytest.mark.django_db


@pytest.fixture
def owner_client(organization_owner_user: RevelUser) -> Client:
    refresh = RefreshToken.for_user(organization_owner_user)
    return Client(HTTP_AUTHORIZATION=f"Bearer {str(refresh.access_token)}")  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# 1. BR-S-02: a standard-rated line needs BT-31, BT-32 alone covers only exempt lines
# ---------------------------------------------------------------------------


class TestStandardLinesNeedTheVatId:
    def test_s_line_with_only_a_tax_number_is_ineligible(
        self, organization: Organization, event: Event, member_user: RevelUser
    ) -> None:
        invoice = make_invoice(
            organization,
            event,
            member_user,
            lines=[line("24.90", "13.00")],
            seller_vat_id="",
            seller_tax_registration_number="12/345/67890",
        )
        assert mapping.ineligibility_reason(invoice) == "missing_seller_vat_id"

    def test_mixed_s_and_e_lines_are_ineligible_too(
        self, organization: Organization, event: Event, member_user: RevelUser
    ) -> None:
        invoice = make_invoice(
            organization,
            event,
            member_user,
            lines=[line("24.90", "13.00"), line("10.00", "0.00", "E")],
            seller_vat_id="",
            seller_tax_registration_number="12/345/67890",
            exempt_reason="x",
        )
        assert mapping.ineligibility_reason(invoice) == "missing_seller_vat_id"

    def test_exempt_only_lines_stay_eligible(
        self, organization: Organization, event: Event, member_user: RevelUser
    ) -> None:
        invoice = make_invoice(
            organization,
            event,
            member_user,
            lines=[line("24.90", "0.00", "E")],
            seller_vat_id="",
            seller_tax_registration_number="12/345/67890",
            exempt_reason="x",
        )
        assert mapping.ineligibility_reason(invoice) is None


# ---------------------------------------------------------------------------
# 2. The VAT-ID endpoints respect the running guard
# ---------------------------------------------------------------------------


class TestVatIdEndpointsGuard:
    def _scheme_org_with_validated_vat_id(self, organization: Organization, country: str = "DE") -> Organization:
        """A scheme organizer (DE or AT) with a validated VAT ID, no tax number and invoicing on."""
        org = _make_org_invoicing_ready(organization)  # validated IT12345678901
        org.vat_country_code = country
        org.vat_id = "DE811234567" if country == "DE" else "ATU12345678"
        org.billing_country_code = country
        org.small_business_scheme = "de_19" if country == "DE" else "at_6_1_27"
        org.vat_rate = Decimal("0.00")
        org.vat_exemption_reason = "note"
        org.tax_registration_number = ""
        org.invoicing_mode = Organization.InvoicingMode.AUTO
        org.save()
        return org

    def test_delete_vat_id_without_a_tax_number_is_refused(
        self, organization: Organization, owner_client: Client
    ) -> None:
        org = self._scheme_org_with_validated_vat_id(organization, "AT")
        response = owner_client.delete(reverse("api:delete_vat_id", kwargs={"slug": org.slug}))
        assert response.status_code == 422, response.content
        assert "Turn invoicing off first" in response.json()["detail"]
        org.refresh_from_db()
        assert org.vat_id == "ATU12345678" and org.vat_id_validated

    def test_delete_vat_id_with_a_tax_number_goes_through(
        self, organization: Organization, owner_client: Client
    ) -> None:
        org = self._scheme_org_with_validated_vat_id(organization, "AT")
        org.tax_registration_number = "12-345/6789"
        org.save()
        assert owner_client.delete(reverse("api:delete_vat_id", kwargs={"slug": org.slug})).status_code == 204

    @pytest.mark.parametrize("invoicing_mode", [Organization.InvoicingMode.AUTO, Organization.InvoicingMode.NONE])
    def test_german_organizer_cannot_delete_its_vat_id(
        self, organization: Organization, owner_client: Client, invoicing_mode: str
    ) -> None:
        """#1258: the Steuernummer does not stand in for a DE Kleinunternehmer's USt-IdNr., invoicing on or off."""
        org = self._scheme_org_with_validated_vat_id(organization)
        org.tax_registration_number = "12/345/67890"
        org.invoicing_mode = invoicing_mode
        org.save()
        response = owner_client.delete(reverse("api:delete_vat_id", kwargs={"slug": org.slug}))
        assert response.status_code == 422, response.content
        assert "USt-IdNr." in response.json()["detail"]
        org.refresh_from_db()
        assert org.vat_id == "DE811234567" and org.vat_id_validated
        org.small_business_scheme = "none"  # the way out: leave the scheme first
        org.save()
        assert owner_client.delete(reverse("api:delete_vat_id", kwargs={"slug": org.slug})).status_code == 204

    def test_set_vat_id_without_a_tax_number_is_decided_by_vies(
        self, organization: Organization, owner_client: Client
    ) -> None:
        """Nothing stands in for the UID, so the swap is decided on the actual VIES outcome, nothing written on no."""
        cache.clear()
        org = self._scheme_org_with_validated_vat_id(organization, "AT")
        invalid = VIESValidationResult(valid=False, name="", address="", request_identifier="")
        with patch("common.service.vies_service.validate_vat_id", return_value=invalid) as vies:
            response = owner_client.put(
                reverse("api:set_vat_id", kwargs={"slug": org.slug}),
                data='{"vat_id": "ATU87654329"}',
                content_type="application/json",
            )
        assert response.status_code == 422, response.content
        assert "Turn invoicing off first" in response.json()["detail"]
        vies.assert_called_once()
        org.refresh_from_db()
        assert org.vat_id == "ATU12345678" and org.vat_id_validated

    def test_non_scheme_org_is_untouched(self, organization: Organization, owner_client: Client) -> None:
        org = _make_org_invoicing_ready(organization)
        org.invoicing_mode = Organization.InvoicingMode.AUTO
        org.save()
        assert owner_client.delete(reverse("api:delete_vat_id", kwargs={"slug": org.slug})).status_code == 204

    def _put_vat_id(self, client: Client, org: Organization, vat_id: str) -> t.Any:
        return client.put(
            reverse("api:set_vat_id", kwargs={"slug": org.slug}),
            data=f'{{"vat_id": "{vat_id}"}}',
            content_type="application/json",
        )

    def test_set_a_foreign_vat_id_is_refused_before_vies(
        self, organization: Organization, owner_client: Client
    ) -> None:
        """The candidate's country follows the new VAT ID's prefix: a DE scheme cannot hold an AT VAT ID (H2)."""
        org = self._scheme_org_with_validated_vat_id(organization)
        org.tax_registration_number = "12/345/67890"
        org.save()
        with patch("common.service.vies_service.validate_vat_id") as vies:
            response = self._put_vat_id(owner_client, org, "ATU12345678")
        assert response.status_code == 422, response.content
        assert "established in Germany" in response.json()["detail"]
        vies.assert_not_called()
        org.refresh_from_db()
        assert org.vat_id == "DE811234567"

    def test_set_a_domestic_vat_id_goes_through(self, organization: Organization, owner_client: Client) -> None:
        org = self._scheme_org_with_validated_vat_id(organization, "AT")
        org.tax_registration_number = "12-345/6789"
        org.save()
        result = VIESValidationResult(valid=True, name="Kleine Bühne", address="Musterstraße 1", request_identifier="r")
        with patch("common.service.vies_service.validate_vat_id", return_value=result) as vies:
            response = self._put_vat_id(owner_client, org, "ATU87654321")
        assert response.status_code == 200, response.content
        vies.assert_called_once()
        org.refresh_from_db()
        assert org.vat_id == "ATU87654321"

    def _lapsed_german_org(self, organization: Organization) -> Organization:
        """A DE Kleinunternehmer with invoicing on whose monthly VIES revalidation flipped the flag."""
        cache.clear()  # no VIES result cached from another test
        org = self._scheme_org_with_validated_vat_id(organization)
        org.tax_registration_number = "12/345/67890"
        org.vat_id_validated = False
        org.vies_request_identifier = "old"
        org.save()
        return org

    @patch(MOCK_RENDER_PDF, return_value=b"fake-pdf")
    @patch(MOCK_SEND_EMAIL)
    def test_lapsed_german_organizer_recovers_by_resubmitting_a_valid_vat_id(
        self,
        _mail: t.Any,
        _pdf: t.Any,
        organization: Organization,
        owner_client: Client,
        event: Event,
        event_ticket_tier: TicketTier,
        member_user: RevelUser,
    ) -> None:
        """#1258: a PUT is decided on the actual VIES outcome, not the pessimistic one, so the organizer can recover."""
        org = self._lapsed_german_org(organization)
        event.vat_country_code = "DE"
        event.save()
        result = VIESValidationResult(valid=True, name="Kleine Bühne", address="Musterstraße 1", request_identifier="r")
        with patch("common.service.vies_service.validate_vat_id", return_value=result) as vies:
            response = self._put_vat_id(owner_client, org, "DE123456789")
        assert response.status_code == 200, response.content
        assert vies.called
        org.refresh_from_db()
        assert org.vat_id == "DE123456789" and org.vat_id_validated
        _create_payment(
            user=member_user,
            event=event,
            tier=event_ticket_tier,
            session_id="cs_recovered",
            buyer_billing_snapshot=_default_billing_snapshot(),
            net_amount=Decimal("100.00"),
            vat_amount=Decimal("0.00"),
            vat_rate=Decimal("0.00"),
        )
        invoice = generate_attendee_invoice("cs_recovered")
        assert invoice is not None and invoice.seller_vat_id == "DE123456789"

    def test_the_guard_decision_is_the_only_vies_call(self, organization: Organization, owner_client: Client) -> None:
        """One request, one VIES decision: a contradicting second lookup must never strand a rejected ID in the row."""
        org = self._lapsed_german_org(organization)
        valid = VIESValidationResult(valid=True, name="Kleine Bühne", address="Musterstraße 1", request_identifier="r")
        invalid = VIESValidationResult(valid=False, name="", address="", request_identifier="")
        with patch("common.service.vies_service.validate_vat_id", side_effect=[valid, invalid]) as vies:
            response = self._put_vat_id(owner_client, org, "DE123456781")
        assert response.status_code == 200, response.content
        assert vies.call_count == 1
        org.refresh_from_db()
        assert (org.vat_id, org.vat_id_validated, org.vies_request_identifier) == ("DE123456781", True, "r")

    def test_invalid_vat_id_leaves_a_lapsed_german_organizer_unchanged(
        self, organization: Organization, owner_client: Client
    ) -> None:
        org = self._lapsed_german_org(organization)
        result = VIESValidationResult(valid=False, name="", address="", request_identifier="")
        with patch("common.service.vies_service.validate_vat_id", return_value=result):
            response = self._put_vat_id(owner_client, org, "DE999999999")
        assert response.status_code == 422, response.content
        assert "USt-IdNr." in response.json()["detail"]
        org.refresh_from_db()
        assert (org.vat_id, org.vat_id_validated, org.vies_request_identifier) == ("DE811234567", False, "old")

    def test_vies_outage_leaves_a_lapsed_german_organizer_unchanged(
        self, organization: Organization, owner_client: Client
    ) -> None:
        org = self._lapsed_german_org(organization)
        with patch("common.service.vies_service.validate_vat_id", side_effect=VIESUnavailableError("down")):
            response = self._put_vat_id(owner_client, org, "DE123456780")
        assert response.status_code == 503, response.content
        assert response.json()["code"] == "vies_unavailable_not_saved"
        org.refresh_from_db()
        assert (org.vat_id, org.vat_id_validated) == ("DE811234567", False)


# ---------------------------------------------------------------------------
# 3. Issuing a draft checks the lines and the seller identity
# ---------------------------------------------------------------------------


class TestDraftIssueGuards:
    def test_pre_scheme_draft_with_vat_lines_cannot_be_issued(
        self, organization: Organization, event: Event, member_user: RevelUser
    ) -> None:
        _make_small_business(organization)
        event.vat_country_code = "DE"
        event.save()
        invoice = _create_draft(organization, event, member_user)  # 22% lines from before the scheme
        with pytest.raises(HttpError) as exc_info:
            issue_draft_invoice(invoice)
        assert exc_info.value.status_code == 422
        invoice.refresh_from_db()
        assert invoice.status == AttendeeInvoice.InvoiceStatus.DRAFT

    def test_draft_cannot_be_issued_with_a_broken_identity(
        self, organization: Organization, event: Event, member_user: RevelUser
    ) -> None:
        org = _make_small_business(organization)
        event.vat_country_code = "DE"
        event.save()
        invoice = _create_draft(organization, event, member_user)
        invoice.line_items = [
            {**invoice.line_items[0], "vat_rate": "0.00", "vat_amount": "0.00", "net_amount": "100.00"}
        ]
        invoice.total_vat = Decimal("0.00")
        invoice.total_net = Decimal("100.00")
        invoice.save()
        Organization.objects.filter(pk=org.pk).update(vat_id_validated=False)  # DE: the USt-IdNr. is the identity
        with pytest.raises(CountryComplianceError):
            issue_draft_invoice(invoice)


# ---------------------------------------------------------------------------
# 4. Credit-note payment fallback follows an exempt invoice
# ---------------------------------------------------------------------------


@patch(MOCK_RENDER_PDF, return_value=b"fake-pdf")
@patch(MOCK_SEND_EMAIL)
def test_credit_note_fallback_follows_the_exempt_invoice(
    _mail: t.Any,
    _pdf: t.Any,
    organization: Organization,
    event: Event,
    event_ticket_tier: TicketTier,
    member_user: RevelUser,
) -> None:
    """The refunded payment's line was replaced by hand in DRAFT, so the credit note falls back to the payment (22%)."""
    org = _make_small_business(organization)
    org.invoicing_mode = Organization.InvoicingMode.HYBRID
    org.save()
    event.vat_country_code = "DE"
    event.save()
    payment = _create_payment(
        user=member_user,
        event=event,
        tier=event_ticket_tier,
        session_id="cs_fb",
        buyer_billing_snapshot=_default_billing_snapshot(),
    )
    draft = generate_attendee_invoice("cs_fb")
    assert draft is not None and [li["vat_category"] for li in draft.line_items] == ["E"]
    hand_line = {
        "description": "Replaced by hand",
        "unit_price_gross": Decimal("100.00"),
        "discount_amount": Decimal("0.00"),
        "net_amount": Decimal("100.00"),
        "vat_amount": Decimal("0.00"),
        "vat_rate": Decimal("0.00"),
    }
    issued = issue_draft_invoice(update_draft_invoice(draft, {"line_items": [hand_line]}))
    assert issued.status == AttendeeInvoice.InvoiceStatus.ISSUED
    note = generate_attendee_credit_note("cs_fb", [payment.id])
    assert note is not None
    assert [(li["vat_category"], li["vat_rate"], li["vat_amount"]) for li in note.line_items] == [("E", "0.00", "0.00")]
    assert note.amount_vat == 0 and note.amount_net == note.amount_gross


# ---------------------------------------------------------------------------
# 5. The live-tier check only guards the none -> scheme transition
# ---------------------------------------------------------------------------


def test_unrelated_billing_patch_of_a_scheme_org_with_a_copied_vat_tier(
    organization: Organization, event: Event, owner_client: Client
) -> None:
    org = _make_small_business(organization)
    TicketTier.objects.create(event=event, name="Copy", price=Decimal("50.00"), vat_rate=Decimal("19.00"))
    response = owner_client.patch(
        reverse("api:update_billing_info", kwargs={"slug": org.slug}),
        data='{"billing_name": "Renamed"}',
        content_type="application/json",
    )
    assert response.status_code == 200, response.content


# ---------------------------------------------------------------------------
# 6. event_compliance with an incomplete scheme identity
# ---------------------------------------------------------------------------


def test_event_compliance_is_blocked_while_the_scheme_identity_is_incomplete(
    organization: Organization, event: Event
) -> None:
    org = _make_small_business(organization)
    org.vat_id_validated = False  # DE (#1258): the Steuernummer it still holds does not stand in
    event.vat_country_code = "DE"
    assert event_compliance(event).attendee_invoicing == AttendeeInvoicingCapability.BLOCKED
    event.is_virtual = True
    assert event_compliance(event).attendee_invoicing == AttendeeInvoicingCapability.BLOCKED


# ---------------------------------------------------------------------------
# 7. Revenue aggregation: 0% is not reverse charge
# ---------------------------------------------------------------------------


class TestRevenueBuckets:
    def _scope(self, org: Organization) -> ReportScope:
        today = timezone.localdate()
        return ReportScope(org=org, event_id=None, date_from=today - datetime.timedelta(days=1), date_to=today)

    def _zero_payment(self, user: RevelUser, event: Event, tier: TicketTier, session: str, rc: bool) -> t.Any:
        return _create_payment(
            user=user,
            event=event,
            tier=tier,
            session_id=session,
            net_amount=Decimal("100.00"),
            vat_amount=Decimal("0.00"),
            vat_rate=Decimal("0.00"),
            buyer_billing_snapshot={**_default_billing_snapshot(rc), "vat_country_code": "FR"},
        )

    def test_exempt_sale_is_not_reverse_charge(
        self, organization: Organization, event: Event, event_ticket_tier: TicketTier, member_user: RevelUser
    ) -> None:
        _make_small_business(organization)
        self._zero_payment(member_user, event, event_ticket_tier, "cs_rev_e", rc=False)
        [section] = build_revenue_report_data(self._scope(organization)).sections
        [bucket] = section.rate_buckets
        assert bucket.vat_rate == 0 and bucket.net == Decimal("100.00")
        assert "reverse" not in bucket.label.lower()
        assert section.transactions[0].reverse_charge is False

    def test_reverse_charge_sale_still_has_its_own_bucket(
        self, organization: Organization, event: Event, event_ticket_tier: TicketTier, member_user: RevelUser
    ) -> None:
        _make_org_invoicing_ready(organization)
        self._zero_payment(member_user, event, event_ticket_tier, "cs_rev_rc", rc=True)
        [section] = build_revenue_report_data(self._scope(organization)).sections
        [bucket] = section.rate_buckets
        assert "reverse" in bucket.label.lower()
        assert section.transactions[0].reverse_charge is True

    def test_exempt_and_reverse_charge_sales_are_separate_buckets(
        self, organization: Organization, event: Event, event_ticket_tier: TicketTier, member_user: RevelUser
    ) -> None:
        _make_org_invoicing_ready(organization)
        self._zero_payment(member_user, event, event_ticket_tier, "cs_rev_a", rc=False)
        self._zero_payment(member_user, event, event_ticket_tier, "cs_rev_b", rc=True)
        [section] = build_revenue_report_data(self._scope(organization)).sections
        assert len(section.rate_buckets) == 2


# ---------------------------------------------------------------------------
# 9. Decimal, not float; malformed input is a 422
# ---------------------------------------------------------------------------


def test_draft_line_guard_uses_decimal_and_rejects_garbage(organization: Organization) -> None:
    org = _make_small_business(organization)
    service.assert_draft_lines_exempt(org, [{"vat_rate": "0.00", "vat_amount": "0E-10"}])
    with pytest.raises(HttpError) as exc_info:
        service.assert_draft_lines_exempt(org, [{"vat_rate": "0.000000001", "vat_amount": "0"}])
    assert exc_info.value.status_code == 422
    with pytest.raises(HttpError) as exc_info:
        service.assert_draft_lines_exempt(org, [{"vat_rate": "abc"}])
    assert exc_info.value.status_code == 422


# ---------------------------------------------------------------------------
# CodeRabbit: ASCII digits only; the statutory note is stamped at issue
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("number", ["١٢٣٤٥٦٧٨٩٠", "１２３４５６７８９０", "12/345/6789٠"])
def test_tax_number_rejects_non_ascii_digits(number: str) -> None:
    assert tax_number_is_valid("DE", number) is False
    assert tax_number_is_valid("DE", "1234567890") is True


class TestStatutoryNoteAtIssue:
    def _exempt_draft(
        self, organization: Organization, event: Event, tier: TicketTier, user: RevelUser, reason: str
    ) -> AttendeeInvoice:
        tier.vat_exemption_reason = reason
        tier.save()
        invoice = _create_draft(organization, event, user)
        line = {
            **invoice.line_items[0],
            "vat_rate": "0.00",
            "vat_amount": "0.00",
            "net_amount": "100.00",
            "vat_category": "E",
            "line_id": "1",
            "tier_id": str(tier.pk),
        }
        invoice.line_items = [line]
        invoice.total_vat = Decimal("0.00")
        invoice.total_net = Decimal("100.00")
        invoice.vat_exemptions = {"E": {"text": reason, "code": ""}}
        invoice.save()
        return invoice

    @patch(MOCK_RENDER_PDF, return_value=b"fake-pdf")
    def test_pre_scheme_exempt_draft_gets_the_note_when_issued(
        self,
        _pdf: t.Any,
        organization: Organization,
        event: Event,
        event_ticket_tier: TicketTier,
        member_user: RevelUser,
    ) -> None:
        """Generated before the scheme (E lines, a tier reason); the scheme is declared, then it is issued."""
        draft = self._exempt_draft(organization, event, event_ticket_tier, member_user, "Art. 132(1)(n) VAT Directive")
        _make_small_business(organization)
        event.vat_country_code = "DE"
        event.save()
        issued = issue_draft_invoice(draft)
        issued.refresh_from_db()
        text = issued.vat_exemptions["E"]["text"]
        assert text.startswith("Steuerbefreit gemäß § 19 UStG")
        assert "Art. 132(1)(n) VAT Directive" in text
        assert issued.line_items == draft.line_items  # the lines themselves are untouched
        assert issued.seller_small_business_scheme == "de_19"  # stamped at issue: the PDF shows no VAT (#1258)

    @patch(MOCK_RENDER_PDF, return_value=b"fake-pdf")
    @patch(MOCK_SEND_EMAIL)
    def test_draft_follows_the_organizer_leaving_the_scheme(
        self,
        _mail: t.Any,
        _pdf: t.Any,
        organization: Organization,
        event: Event,
        event_ticket_tier: TicketTier,
        member_user: RevelUser,
    ) -> None:
        """Generated under de_19 (E lines, the §19 note); the organizer leaves the scheme; then it is issued."""
        org = _make_small_business(organization)  # validated DE VAT ID
        org.invoicing_mode = Organization.InvoicingMode.HYBRID
        org.save()
        event.vat_country_code = "DE"
        event.save()
        _create_payment(
            user=member_user,
            event=event,
            tier=event_ticket_tier,
            session_id="cs_left",
            buyer_billing_snapshot=_default_billing_snapshot(),
            net_amount=Decimal("100.00"),
            vat_amount=Decimal("0.00"),
            vat_rate=Decimal("0.00"),
        )
        draft = generate_attendee_invoice("cs_left")
        assert draft is not None and draft.seller_small_business_scheme == "de_19"
        assert draft.vat_exemptions["E"]["text"].startswith("Steuerbefreit gemäß § 19 UStG")
        org.small_business_scheme = "none"
        org.vat_exemption_reason = "Own exemption"
        org.save()
        issued = issue_draft_invoice(draft)
        issued.refresh_from_db()
        assert issued.seller_small_business_scheme == "none"
        assert issued.vat_exemptions["E"]["text"] == "Own exemption"
        assert issued.line_items == draft.line_items  # the lines stay as they were (E at 0%)
        html = render_to_string(
            "invoices/attendee_invoice.html",
            {"invoice": issued, "is_draft": False, "is_export": False, "is_virtual_interim": False},
        )
        assert "<th>VAT</th>" in html and "Subtotal" in html  # the normal layout is back
        assert "Steuerbefreit gemäß § 19 UStG" not in html

    @patch(MOCK_RENDER_PDF, return_value=b"fake-pdf")
    @patch(MOCK_SEND_EMAIL)
    def test_identifiers_follow_the_organizer_leaving_the_scheme(
        self,
        _mail: t.Any,
        _pdf: t.Any,
        organization: Organization,
        event: Event,
        event_ticket_tier: TicketTier,
        member_user: RevelUser,
    ) -> None:
        """An AT draft (tax number, no UID); the organizer leaves the scheme with a validated UID; then issued."""
        org = _make_small_business(organization, "at_6_1_27")
        org.invoicing_mode = Organization.InvoicingMode.HYBRID
        org.save()
        event.vat_country_code = "AT"
        event.save()
        _create_payment(
            user=member_user,
            event=event,
            tier=event_ticket_tier,
            session_id="cs_left_at",
            buyer_billing_snapshot=_default_billing_snapshot(),
            net_amount=Decimal("100.00"),
            vat_amount=Decimal("0.00"),
            vat_rate=Decimal("0.00"),
        )
        draft = generate_attendee_invoice("cs_left_at")
        assert draft is not None and (draft.seller_vat_id, draft.seller_tax_registration_number) == ("", "12-345/6789")
        org.small_business_scheme = "none"
        org.vat_id, org.vat_country_code, org.vat_id_validated = "ATU12345678", "AT", True
        org.vat_exemption_reason = "Own exemption"
        org.save()
        issued = issue_draft_invoice(draft)
        issued.refresh_from_db()
        assert issued.seller_small_business_scheme == "none"
        assert issued.seller_vat_id == "ATU12345678"
        assert issued.seller_tax_registration_number == "12-345/6789"
        assert issued.seller_vat_country == "AT"
        html = render_to_string(
            "invoices/attendee_invoice.html",
            {"invoice": issued, "is_draft": False, "is_export": False, "is_virtual_interim": False},
        )
        assert "VAT: ATU12345678" in html and "<th>VAT</th>" in html and "Subtotal" in html

    @patch(MOCK_RENDER_PDF, return_value=b"fake-pdf")
    @patch(MOCK_SEND_EMAIL)
    def test_identifiers_are_refreshed_without_an_event_too(
        self,
        _mail: t.Any,
        _pdf: t.Any,
        organization: Organization,
        event: Event,
        event_ticket_tier: TicketTier,
        member_user: RevelUser,
    ) -> None:
        """The event was deleted since the sale: the VAT ID and tax number still follow, the VAT country stays.

        Exercised on the helper: ``AttendeeInvoice.event`` is ``null=True`` without ``blank=True``, so
        ``full_clean()`` (run by every ``save``) refuses an event-less row today, a pre-existing gap.
        """
        org = _make_small_business(organization, "at_6_1_27")
        org.invoicing_mode = Organization.InvoicingMode.HYBRID
        org.save()
        event.vat_country_code = "AT"
        event.save()
        _create_payment(
            user=member_user,
            event=event,
            tier=event_ticket_tier,
            session_id="cs_left_noev",
            buyer_billing_snapshot=_default_billing_snapshot(),
            net_amount=Decimal("100.00"),
            vat_amount=Decimal("0.00"),
            vat_rate=Decimal("0.00"),
        )
        draft = generate_attendee_invoice("cs_left_noev")
        assert draft is not None and draft.seller_vat_country == "AT"
        draft.event = None
        org.small_business_scheme = "none"
        org.vat_id, org.vat_country_code, org.vat_id_validated = "ATU12345678", "AT", True
        org.vat_exemption_reason = "Own exemption"
        org.save()
        draft.organization = org
        fields = _follow_organizer_scheme(draft)
        assert {"seller_vat_id", "seller_tax_registration_number", "seller_small_business_scheme"} <= set(fields)
        assert (draft.seller_vat_id, draft.seller_tax_registration_number) == ("ATU12345678", "12-345/6789")
        assert draft.seller_vat_country == "AT"  # kept: nothing to derive it from
        assert draft.seller_small_business_scheme == "none"

    @patch(MOCK_RENDER_PDF, return_value=b"fake-pdf")
    @patch(MOCK_SEND_EMAIL)
    def test_hand_entered_line_takes_the_current_reason_across_a_scheme_change(
        self,
        _mail: t.Any,
        _pdf: t.Any,
        organization: Organization,
        event: Event,
        event_ticket_tier: TicketTier,
        member_user: RevelUser,
    ) -> None:
        """A line replaced by hand (no provenance) under de_19 carries the §19 note; the organizer leaves; issued."""
        org = _make_small_business(organization)
        org.invoicing_mode = Organization.InvoicingMode.HYBRID
        org.save()
        event.vat_country_code = "DE"
        event.save()
        _create_payment(
            user=member_user,
            event=event,
            tier=event_ticket_tier,
            session_id="cs_hand",
            buyer_billing_snapshot=_default_billing_snapshot(),
            net_amount=Decimal("100.00"),
            vat_amount=Decimal("0.00"),
            vat_rate=Decimal("0.00"),
        )
        draft = generate_attendee_invoice("cs_hand")
        assert draft is not None
        hand_line = {
            "description": "Replaced by hand",
            "unit_price_gross": Decimal("100.00"),
            "discount_amount": Decimal("0.00"),
            "net_amount": Decimal("100.00"),
            "vat_amount": Decimal("0.00"),
            "vat_rate": Decimal("0.00"),
        }
        draft = update_draft_invoice(draft, {"line_items": [hand_line]})
        draft.refresh_from_db()
        assert "line_id" not in draft.line_items[0]
        assert draft.vat_exemptions["E"]["text"].startswith("Steuerbefreit gemäß § 19 UStG")
        org.small_business_scheme = "none"
        org.vat_exemption_reason = "Own exemption"
        org.save()
        issued = issue_draft_invoice(draft)
        issued.refresh_from_db()
        assert issued.vat_exemptions["E"]["text"] == "Own exemption"
        assert "§ 19" not in issued.vat_exemptions["E"]["text"]

    @patch(MOCK_RENDER_PDF, return_value=b"fake-pdf")
    def test_identifiers_follow_the_organizer_joining_the_scheme(
        self,
        _pdf: t.Any,
        organization: Organization,
        event: Event,
        event_ticket_tier: TicketTier,
        member_user: RevelUser,
    ) -> None:
        """A pre-scheme draft (Italian VIES identity); the organizer becomes a DE Kleinunternehmer; then issued."""
        _make_org_invoicing_ready(organization)
        draft = self._exempt_draft(organization, event, event_ticket_tier, member_user, "Art. 132(1)(n) VAT Directive")
        draft.seller_vat_id, draft.seller_vat_country = "IT12345678901", "IT"
        draft.save()
        _make_small_business(organization)  # de_19 with the validated DE811234567 and a Steuernummer
        event.vat_country_code = "DE"
        event.save()
        issued = issue_draft_invoice(draft)
        issued.refresh_from_db()
        assert issued.seller_small_business_scheme == "de_19"
        assert (issued.seller_vat_id, issued.seller_tax_registration_number, issued.seller_vat_country) == (
            "DE811234567",
            "12/345/67890",
            "DE",
        )

    @patch(MOCK_RENDER_PDF, return_value=b"fake-pdf")
    def test_non_scheme_draft_is_unchanged(
        self,
        _pdf: t.Any,
        organization: Organization,
        event: Event,
        event_ticket_tier: TicketTier,
        member_user: RevelUser,
    ) -> None:
        _make_org_invoicing_ready(organization)
        draft = self._exempt_draft(organization, event, event_ticket_tier, member_user, "Art. 132(1)(n) VAT Directive")
        issued = issue_draft_invoice(draft)
        issued.refresh_from_db()
        assert issued.vat_exemptions == {"E": {"text": "Art. 132(1)(n) VAT Directive", "code": ""}}


@pytest.mark.django_db
class TestValidatedSchemeCountryMismatch:
    """A validated VAT ID does not exempt a scheme organizer from the scheme-country rule (#1244)."""

    def test_validated_mismatch_is_blocked_at_generation(self, organization: Organization) -> None:
        org = _make_small_business(organization)  # de_19
        # Reached only around the guards (e.g. an ORM write): an Italian VIES-validated VAT ID.
        org.vat_id, org.vat_country_code, org.vat_id_validated = "IT12345678901", "IT", True
        org.save()

        assert service.scheme_path_identity_errors(org)
        assert not service.seller_identity_decision(org).allowed

    def test_validated_mismatch_blocks_event_compliance(self, organization: Organization, event: Event) -> None:
        org = _make_small_business(organization)
        org.vat_id, org.vat_country_code, org.vat_id_validated = "IT12345678901", "IT", True
        org.save()
        event.vat_country_code = "DE"
        assert event_compliance(event).attendee_invoicing == AttendeeInvoicingCapability.BLOCKED

    def test_validated_matching_country_stays_allowed(self, organization: Organization) -> None:
        org = _make_small_business(organization)
        org.vat_id, org.vat_country_code, org.vat_id_validated = "DE123456789", "DE", True
        org.save()

        assert service.scheme_path_identity_errors(org) == []
        assert service.seller_identity_decision(org).allowed
