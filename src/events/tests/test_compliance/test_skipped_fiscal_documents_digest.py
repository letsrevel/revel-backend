"""The daily FISCAL_DOCUMENT_SKIPPED digest to organizers (#1073)."""

import typing as t
from decimal import Decimal
from unittest.mock import patch

import pytest
from django.utils import translation
from freezegun import freeze_time

from accounts.models import RevelUser
from events.models import (
    Event,
    Organization,
    OrganizationStaff,
    Payment,
    PermissionMap,
    PermissionsSchema,
    Refund,
    SkippedFiscalDocument,
    TicketTier,
)
from events.service.attendee_invoice_service import generate_attendee_credit_note, generate_attendee_invoice
from events.service.skipped_fiscal_document_service import notify_skipped_documents
from events.tasks import notify_skipped_fiscal_documents_task
from events.tests.test_attendee_invoice._helpers import (
    MOCK_RENDER_PDF,
    _create_issued,
    _create_payment,
    _default_billing_snapshot,
    _make_org_invoicing_ready,
)
from notifications.enums import TRANSACTIONAL_TYPES, NotificationType
from notifications.models import Notification
from notifications.service.templates.registry import get_template

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("_no_pdf")]

TYPE = NotificationType.FISCAL_DOCUMENT_SKIPPED


@pytest.fixture
def _no_pdf() -> t.Iterator[None]:
    with patch(MOCK_RENDER_PDF, return_value=b"fake-pdf"):
        yield


def _ready_in(org: Organization, country: str) -> Organization:
    _make_org_invoicing_ready(org)
    org.vat_country_code = country
    org.vat_id = f"{country}123456789"
    org.invoicing_mode = Organization.InvoicingMode.AUTO
    org.save()
    return org


def _business(vat_id: str) -> dict[str, t.Any]:
    return {**_default_billing_snapshot(), "vat_id": vat_id, "vat_country_code": vat_id[:2], "vat_id_status": "valid"}


def _consumer() -> dict[str, t.Any]:
    return {**_default_billing_snapshot(), "vat_id": "", "vat_country_code": "", "billing_name": "Jane Doe"}


def _digests() -> list[Notification]:
    return list(Notification.objects.filter(notification_type=TYPE).order_by("created_at"))


@pytest.fixture
def sale(event: Event, event_ticket_tier: TicketTier, member_user: RevelUser) -> t.Callable[..., Payment]:
    def make(snapshot: dict[str, t.Any], session_id: str = "cs_test_123") -> Payment:
        return _create_payment(
            user=member_user,
            event=event,
            tier=event_ticket_tier,
            session_id=session_id,
            buyer_billing_snapshot=snapshot,
        )

    return make


def _refund_all(payment: Payment) -> None:
    row = Refund.objects.create(
        payment=payment,
        amount=payment.amount,
        currency="EUR",
        status=Refund.RefundStatus.SUCCEEDED,
        source=Refund.Source.ORGANIZER_API,
    )
    generate_attendee_credit_note(payment.stripe_session_id, [payment.id], refund_ids=[row.id])


def _issued_pre_gate(org: Organization, event: Event, user: RevelUser, payment: Payment, country: str) -> None:
    invoice = _create_issued(org, event, user)
    invoice.stripe_session_id = payment.stripe_session_id
    invoice.seller_vat_country = country
    invoice.buyer_vat_id = (payment.buyer_billing_snapshot or {}).get("vat_id", "")
    invoice.save(update_fields=["stripe_session_id", "seller_vat_country", "buyer_vat_id"])


class TestSkipPathsReachTheOrganizer:
    def test_spain_after_the_flip(
        self, organization: Organization, organization_owner_user: RevelUser, sale: t.Callable[..., Payment]
    ) -> None:
        _ready_in(organization, "ES")
        sale(_consumer())
        with freeze_time("2027-01-02"):
            generate_attendee_invoice("cs_test_123")

        assert notify_skipped_documents() == 1

        (digest,) = _digests()
        assert digest.user == organization_owner_user
        assert (digest.context["invoice_count"], digest.context["credit_note_count"]) == (1, 0)
        assert digest.context["items"][0]["policy_country"] == "ES"

    @pytest.mark.parametrize("country", ["BE", "PL"])
    def test_pre_gate_b2b_invoice_refunded(
        self,
        organization: Organization,
        event: Event,
        member_user: RevelUser,
        sale: t.Callable[..., Payment],
        country: str,
    ) -> None:
        _ready_in(organization, country)
        payment = sale(_business(f"{country}0123456789"))
        _issued_pre_gate(organization, event, member_user, payment, country)
        _refund_all(payment)

        notify_skipped_documents()

        (digest,) = _digests()
        assert (digest.context["invoice_count"], digest.context["credit_note_count"]) == (0, 1)
        assert digest.context["items"][0]["kind"] == "credit_note"

    @pytest.mark.parametrize("country", ["HR", "PT", "RO", "SI", "GR", "HU"])
    def test_fiscalized_countries(
        self, organization: Organization, sale: t.Callable[..., Payment], country: str
    ) -> None:
        _ready_in(organization, country)
        sale(_consumer())
        generate_attendee_invoice("cs_test_123")

        notify_skipped_documents()

        (digest,) = _digests()
        assert digest.context["items"][0]["policy_country"] == country

    def test_credit_note_on_a_skipped_invoice_joins_the_digest(
        self, organization: Organization, sale: t.Callable[..., Payment]
    ) -> None:
        _ready_in(organization, "BE")
        payment = sale(_business("BE0123456789"))
        generate_attendee_invoice("cs_test_123")
        _refund_all(payment)

        notify_skipped_documents()

        (digest,) = _digests()
        context = digest.context
        assert (context["invoice_count"], context["credit_note_count"]) == (1, 1)
        assert (context["invoice_totals"], context["credit_note_totals"]) == (["EUR 100.00"], ["EUR 100.00"])
        assert {item["kind"] for item in context["items"]} == {"invoice", "credit_note"}


class TestSweep:
    def test_stamps_rows_and_is_idempotent(self, organization: Organization, sale: t.Callable[..., Payment]) -> None:
        _ready_in(organization, "BE")
        sale(_business("BE0123456789"))
        generate_attendee_invoice("cs_test_123")

        assert notify_skipped_fiscal_documents_task() == 1
        assert notify_skipped_fiscal_documents_task() == 0

        assert len(_digests()) == 1
        assert SkippedFiscalDocument.objects.get().notified_at is not None

    def test_later_skips_get_their_own_digest(self, organization: Organization, sale: t.Callable[..., Payment]) -> None:
        _ready_in(organization, "BE")
        sale(_business("BE0123456789"), "cs_one")
        generate_attendee_invoice("cs_one")
        notify_skipped_documents()
        sale(_business("BE0123456789"), "cs_two")
        generate_attendee_invoice("cs_two")

        notify_skipped_documents()

        assert [d.context["invoice_count"] for d in _digests()] == [1, 1]

    def test_one_digest_per_organization_with_capped_items(
        self, organization: Organization, sale: t.Callable[..., Payment]
    ) -> None:
        _ready_in(organization, "PL")
        for i in range(12):
            sale(_business("NL123456789B01"), f"cs_{i}")
            generate_attendee_invoice(f"cs_{i}")

        notify_skipped_documents()

        (digest,) = _digests()
        assert digest.context["invoice_count"] == 12
        assert len(digest.context["items"]) == 10
        assert digest.context["more_count"] == 2
        assert digest.context["invoice_totals"] == [f"EUR {Decimal('1200.00')}"]
        assert digest.context["credit_note_totals"] == []

    def test_documents_of_a_deleted_organization_are_not_notified(
        self, organization: Organization, sale: t.Callable[..., Payment]
    ) -> None:
        _ready_in(organization, "BE")
        sale(_business("BE0123456789"))
        generate_attendee_invoice("cs_test_123")
        SkippedFiscalDocument.objects.update(organization=None)

        assert notify_skipped_documents() == 0
        assert _digests() == []

    def test_type_is_not_transactional(self) -> None:
        """Follows digest preferences and can be muted, unlike money receipts."""
        assert TYPE not in TRANSACTIONAL_TYPES


def _staff(organization: Organization, user: RevelUser, **permissions: bool) -> None:
    OrganizationStaff.objects.create(
        organization=organization,
        user=user,
        permissions=PermissionsSchema(default=PermissionMap(**permissions)).model_dump(mode="json"),
    )


class TestRecipients:
    def test_owner_gets_the_list_and_ticket_staff_the_tickets(
        self,
        organization: Organization,
        event: Event,
        organization_owner_user: RevelUser,
        organization_staff_user: RevelUser,
        public_user: RevelUser,
        sale: t.Callable[..., Payment],
    ) -> None:
        _ready_in(organization, "BE")
        _staff(organization, organization_staff_user, manage_tickets=True)
        _staff(organization, public_user, manage_tickets=False)
        sale(_business("BE0123456789"))
        generate_attendee_invoice("cs_test_123")

        notify_skipped_documents()

        by_user = {d.user_id: d.context for d in _digests()}
        assert set(by_user) == {organization_owner_user.id, organization_staff_user.id}
        owner, staff = by_user[organization_owner_user.id], by_user[organization_staff_user.id]
        assert owner["is_owner"] is True
        assert owner["action_url"].endswith(f"/org/{organization.slug}/admin/billing/skipped-documents")
        assert staff["is_owner"] is False
        assert staff["action_url"].endswith(
            f"/org/{organization.slug}/admin/events/{event.id}/tickets?invoice_skipped=true"
        )

    def test_a_muted_type_is_not_sent(
        self, organization: Organization, organization_owner_user: RevelUser, sale: t.Callable[..., Payment]
    ) -> None:
        prefs = organization_owner_user.notification_preferences
        prefs.notification_type_settings = {TYPE: {"enabled": False}}
        prefs.save()
        _ready_in(organization, "BE")
        sale(_business("BE0123456789"))
        generate_attendee_invoice("cs_test_123")

        notify_skipped_documents()

        assert _digests() == []
        assert SkippedFiscalDocument.objects.get().notified_at is not None


class TestRendering:
    @pytest.mark.parametrize("language", ["en", "de", "it", "es", "fr", "pt"])
    def test_every_channel_renders_for_owner_and_staff(
        self,
        organization: Organization,
        organization_staff_user: RevelUser,
        sale: t.Callable[..., Payment],
        language: str,
    ) -> None:
        _ready_in(organization, "BE")
        _staff(organization, organization_staff_user, manage_tickets=True)
        payment = sale(_business("BE0123456789"))
        generate_attendee_invoice("cs_test_123")
        _refund_all(payment)
        notify_skipped_documents()
        template = get_template(TYPE)

        with translation.override(language):
            for digest in _digests():
                title = template.get_in_app_title(digest)
                assert organization.name in title and "2" in title
                for body in (
                    template.get_in_app_body(digest),
                    template.get_email_text_body(digest),
                    template.get_email_html_body(digest) or "",
                    template.get_telegram_body(digest),
                ):
                    assert "BE0123456789" in body
                    assert "EUR 100.00" in body
                    assert digest.context["action_url"] in body.replace("&amp;", "&")
                assert template.get_email_subject(digest)

    def test_english_copy(self, organization: Organization, sale: t.Callable[..., Payment]) -> None:
        _ready_in(organization, "BE")
        sale(_business("BE0123456789"))
        generate_attendee_invoice("cs_test_123")
        notify_skipped_documents()
        (digest,) = _digests()
        template = get_template(TYPE)

        with translation.override("en"):
            assert template.get_email_subject(digest) == f"{organization.name}: 1 document to issue yourself"
            body = template.get_in_app_body(digest)
        assert "Revel did not issue this document" in body
        assert "Invoices: 1 (EUR 100.00). Credit notes: 0 (-)." in body
        assert "Open the documents to issue yourself" in body

    def test_translated_copy(self, organization: Organization, sale: t.Callable[..., Payment]) -> None:
        _ready_in(organization, "BE")
        sale(_business("BE0123456789"))
        generate_attendee_invoice("cs_test_123")
        notify_skipped_documents()
        (digest,) = _digests()
        template = get_template(TYPE)

        with translation.override("it"):
            assert template.get_email_subject(digest) == f"{organization.name}: 1 documento da emettere in proprio"
            assert "Revel non ha emesso questo documento" in template.get_email_text_body(digest)
