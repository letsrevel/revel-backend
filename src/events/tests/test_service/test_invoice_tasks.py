"""Tests for VAT/invoice-related Celery tasks.

Tests cover:
- generate_monthly_invoices_task: invoice generation dispatch and email fan-out
- send_invoice_email_task: email delivery with PDF attachment, edge cases
- revalidate_vat_ids_task: per-org task dispatch for all orgs with VAT IDs
- revalidate_single_vat_id_task: individual org VIES re-validation
"""

from datetime import date
from decimal import Decimal
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from django.utils import timezone

from accounts.models import RevelUser
from common.models import SiteSettings
from events.models import Organization
from events.models.invoice import PlatformFeeInvoice
from events.tasks import (
    generate_monthly_invoices_task,
    revalidate_single_vat_id_task,
    revalidate_vat_ids_task,
    send_invoice_email_task,
)

pytestmark = pytest.mark.django_db


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def invoice_owner(django_user_model: type[RevelUser]) -> RevelUser:
    """User who owns organizations used in invoice tests."""
    return django_user_model.objects.create_user(
        username="invoice_owner",
        email="invoice_owner@example.com",
        password="pass",
    )


@pytest.fixture
def invoice_org(invoice_owner: RevelUser) -> Organization:
    """Organization for invoice task tests."""
    return Organization.objects.create(
        name="Invoice Test Org",
        slug="invoice-test-org",
        owner=invoice_owner,
        vat_id="IT12345678901",
        billing_email="billing@invoicetest.com",
    )


@pytest.fixture
def invoice_org_no_billing_email(invoice_owner: RevelUser) -> Organization:
    """Organization without billing email (falls back to contact_email)."""
    return Organization.objects.create(
        name="No Billing Org",
        slug="no-billing-org",
        owner=invoice_owner,
        contact_email="contact@nobilling.com",
    )


@pytest.fixture
def site_settings() -> SiteSettings:
    """SiteSettings singleton with platform details configured."""
    site = SiteSettings.get_solo()
    site.platform_business_name = "Revel S.r.l."
    site.platform_business_address = "Via Test 1, Roma"
    site.platform_vat_id = "IT99999999999"
    site.platform_invoice_bcc_email = "accounting@revel.test"
    site.frontend_base_url = "https://app.revel.test"
    site.save()
    return site


@pytest.fixture
def sample_invoice(invoice_org: Organization, site_settings: SiteSettings) -> PlatformFeeInvoice:
    """A fully populated PlatformFeeInvoice with a mock PDF file."""
    now = timezone.now()
    invoice = PlatformFeeInvoice.objects.create(
        organization=invoice_org,
        invoice_number="RVL-2026-000001",
        period_start=date(2026, 1, 1),
        period_end=date(2026, 1, 31),
        fee_gross=Decimal("100.00"),
        fee_net=Decimal("81.97"),
        fee_vat=Decimal("18.03"),
        fee_vat_rate=Decimal("22.00"),
        currency="EUR",
        reverse_charge=False,
        org_name=invoice_org.name,
        org_vat_id=invoice_org.vat_id,
        org_vat_country="IT",
        org_address="Via Roma 1",
        platform_business_name=site_settings.platform_business_name,
        platform_business_address=site_settings.platform_business_address,
        platform_vat_id=site_settings.platform_vat_id,
        total_tickets=10,
        total_ticket_revenue=Decimal("1000.00"),
        status=PlatformFeeInvoice.InvoiceStatus.ISSUED,
        issued_at=now,
    )
    # Simulate a PDF file being attached
    from django.core.files.base import ContentFile

    invoice.pdf_file.save("RVL-2026-000001.pdf", ContentFile(b"%PDF-fake"), save=True)
    return invoice


@pytest.fixture
def sample_invoice_no_pdf(invoice_org: Organization, site_settings: SiteSettings) -> PlatformFeeInvoice:
    """A PlatformFeeInvoice without a PDF file attached."""
    return PlatformFeeInvoice.objects.create(
        organization=invoice_org,
        invoice_number="RVL-2026-000002",
        period_start=date(2026, 2, 1),
        period_end=date(2026, 2, 28),
        fee_gross=Decimal("50.00"),
        fee_net=Decimal("40.98"),
        fee_vat=Decimal("9.02"),
        fee_vat_rate=Decimal("22.00"),
        currency="EUR",
        org_name=invoice_org.name,
        org_vat_id=invoice_org.vat_id,
        org_vat_country="IT",
        org_address="Via Roma 1",
        platform_business_name=site_settings.platform_business_name,
        platform_business_address=site_settings.platform_business_address,
        platform_vat_id=site_settings.platform_vat_id,
        status=PlatformFeeInvoice.InvoiceStatus.ISSUED,
        issued_at=timezone.now(),
    )


# ===========================================================================
# generate_monthly_invoices_task
# ===========================================================================


class TestGenerateMonthlyInvoicesTask:
    """Tests for the generate_monthly_invoices_task Celery task."""

    @patch("events.tasks.send_invoice_email_task.delay")
    @patch("events.service.invoice_service.generate_monthly_invoices")
    def test_generates_invoices_and_dispatches_emails(
        self,
        mock_generate: MagicMock,
        mock_email_delay: MagicMock,
        sample_invoice: PlatformFeeInvoice,
    ) -> None:
        """Task generates invoices and dispatches email tasks for those with PDFs."""
        mock_generate.return_value = [sample_invoice]

        result = generate_monthly_invoices_task()

        mock_generate.assert_called_once()
        mock_email_delay.assert_called_once_with(str(sample_invoice.id))
        assert result == {"invoices_generated": 1}

    @patch("events.tasks.send_invoice_email_task.delay")
    @patch("events.service.invoice_service.generate_monthly_invoices")
    def test_skips_email_for_invoices_without_pdf(
        self,
        mock_generate: MagicMock,
        mock_email_delay: MagicMock,
        sample_invoice_no_pdf: PlatformFeeInvoice,
    ) -> None:
        """Invoices without a PDF file do not get email tasks dispatched."""
        mock_generate.return_value = [sample_invoice_no_pdf]

        result = generate_monthly_invoices_task()

        mock_generate.assert_called_once()
        mock_email_delay.assert_not_called()
        assert result == {"invoices_generated": 1}

    @patch("events.tasks.send_invoice_email_task.delay")
    @patch("events.service.invoice_service.generate_monthly_invoices")
    def test_no_invoices_generated(
        self,
        mock_generate: MagicMock,
        mock_email_delay: MagicMock,
    ) -> None:
        """When no invoices are generated, no emails are dispatched."""
        mock_generate.return_value = []

        result = generate_monthly_invoices_task()

        mock_generate.assert_called_once()
        mock_email_delay.assert_not_called()
        assert result == {"invoices_generated": 0}

    @patch("events.tasks.send_invoice_email_task.delay")
    @patch("events.service.invoice_service.generate_monthly_invoices")
    def test_multiple_invoices_dispatch_individual_emails(
        self,
        mock_generate: MagicMock,
        mock_email_delay: MagicMock,
        sample_invoice: PlatformFeeInvoice,
    ) -> None:
        """Each invoice with a PDF gets its own email task dispatched."""
        # Create a second invoice-like mock with a PDF
        invoice2 = MagicMock()
        invoice2.id = uuid4()
        invoice2.pdf_file = MagicMock()
        invoice2.pdf_file.__bool__ = lambda self: True
        invoice2.organization_id = sample_invoice.organization_id

        mock_generate.return_value = [sample_invoice, invoice2]

        result = generate_monthly_invoices_task()

        assert mock_email_delay.call_count == 2
        assert result == {"invoices_generated": 2}

    @patch("events.tasks.send_invoice_email_task.delay")
    @patch("events.service.invoice_service.generate_monthly_invoices")
    def test_skips_invoice_with_null_organization(
        self,
        mock_generate: MagicMock,
        mock_email_delay: MagicMock,
    ) -> None:
        """Invoices with organization_id=None (org deleted) do not get email tasks."""
        invoice = MagicMock()
        invoice.pdf_file = MagicMock()
        invoice.pdf_file.__bool__ = lambda self: True
        invoice.organization_id = None

        mock_generate.return_value = [invoice]

        result = generate_monthly_invoices_task()

        mock_email_delay.assert_not_called()
        assert result == {"invoices_generated": 1}


# ===========================================================================
# send_invoice_email_task
# ===========================================================================


class TestSendInvoiceEmailTask:
    """Tests for the send_invoice_email_task Celery task."""

    @patch("events.tasks.send_email")
    def test_sends_email_to_correct_recipients(
        self,
        mock_send_email: MagicMock,
        sample_invoice: PlatformFeeInvoice,
        site_settings: SiteSettings,
    ) -> None:
        """Email is sent to org owner and billing email with correct subject and body."""
        send_invoice_email_task(str(sample_invoice.id))

        mock_send_email.assert_called_once()
        call_kwargs = mock_send_email.call_args.kwargs

        # Recipients should include owner email and billing email
        assert "invoice_owner@example.com" in call_kwargs["to"]
        assert "billing@invoicetest.com" in call_kwargs["to"]

        # Subject includes invoice number and currency
        assert "RVL-2026-000001" in call_kwargs["subject"]
        assert "EUR" in call_kwargs["subject"]

        # Body includes currency and period dates
        assert "EUR" in call_kwargs["body"]
        assert "2026-01-01" in call_kwargs["body"]
        assert "2026-01-31" in call_kwargs["body"]

    @patch("events.tasks.send_email")
    def test_attaches_pdf_file(
        self,
        mock_send_email: MagicMock,
        sample_invoice: PlatformFeeInvoice,
        site_settings: SiteSettings,
    ) -> None:
        """The PDF file is attached to the email."""
        send_invoice_email_task(str(sample_invoice.id))

        call_kwargs = mock_send_email.call_args.kwargs
        assert call_kwargs["attachment_storage_path"] == sample_invoice.pdf_file.name
        assert call_kwargs["attachment_filename"] == "RVL-2026-000001.pdf"

    @patch("events.tasks.send_email")
    def test_includes_bcc_from_site_settings(
        self,
        mock_send_email: MagicMock,
        sample_invoice: PlatformFeeInvoice,
        site_settings: SiteSettings,
    ) -> None:
        """BCC includes the platform invoice BCC email from site settings."""
        send_invoice_email_task(str(sample_invoice.id))

        call_kwargs = mock_send_email.call_args.kwargs
        assert call_kwargs["bcc"] == ["accounting@revel.test"]

    @patch("events.tasks.send_email")
    def test_no_bcc_when_site_setting_is_empty(
        self,
        mock_send_email: MagicMock,
        sample_invoice: PlatformFeeInvoice,
        site_settings: SiteSettings,
    ) -> None:
        """When platform_invoice_bcc_email is empty, BCC list is empty."""
        site_settings.platform_invoice_bcc_email = ""
        site_settings.save()

        send_invoice_email_task(str(sample_invoice.id))

        call_kwargs = mock_send_email.call_args.kwargs
        assert call_kwargs["bcc"] == []

    @patch("events.tasks.send_email")
    def test_handles_deleted_organization_gracefully(
        self,
        mock_send_email: MagicMock,
        sample_invoice: PlatformFeeInvoice,
        site_settings: SiteSettings,
    ) -> None:
        """When the organization has been deleted (SET_NULL), the task returns without sending."""
        # SET_NULL the org relationship
        PlatformFeeInvoice.objects.filter(pk=sample_invoice.pk).update(organization=None)

        send_invoice_email_task(str(sample_invoice.id))

        mock_send_email.assert_not_called()

    @patch("events.tasks.send_email")
    def test_handles_no_recipients_gracefully(
        self,
        mock_send_email: MagicMock,
        site_settings: SiteSettings,
        django_user_model: type[RevelUser],
    ) -> None:
        """When there are no email recipients, the task returns without sending."""
        # Create an owner with no email
        owner_no_email = django_user_model.objects.create_user(
            username="no_email_owner",
            email="",
            password="pass",
        )
        org = Organization.objects.create(
            name="No Email Org",
            slug="no-email-org",
            owner=owner_no_email,
            billing_email="",
            contact_email=None,
        )
        invoice = PlatformFeeInvoice.objects.create(
            organization=org,
            invoice_number="RVL-2026-000099",
            period_start=date(2026, 1, 1),
            period_end=date(2026, 1, 31),
            fee_gross=Decimal("10.00"),
            fee_net=Decimal("8.20"),
            fee_vat=Decimal("1.80"),
            fee_vat_rate=Decimal("22.00"),
            org_name=org.name,
            org_vat_id="",
            platform_business_name="Revel",
            platform_business_address="Test",
            platform_vat_id="IT99999999999",
            status=PlatformFeeInvoice.InvoiceStatus.ISSUED,
            issued_at=timezone.now(),
        )
        from django.core.files.base import ContentFile

        invoice.pdf_file.save("test.pdf", ContentFile(b"%PDF-fake"), save=True)

        send_invoice_email_task(str(invoice.id))

        mock_send_email.assert_not_called()

    @patch("events.tasks.send_email")
    def test_falls_back_to_contact_email_when_no_billing_email(
        self,
        mock_send_email: MagicMock,
        invoice_org_no_billing_email: Organization,
        site_settings: SiteSettings,
    ) -> None:
        """When billing_email is empty, contact_email is used as fallback recipient."""
        invoice = PlatformFeeInvoice.objects.create(
            organization=invoice_org_no_billing_email,
            invoice_number="RVL-2026-000050",
            period_start=date(2026, 3, 1),
            period_end=date(2026, 3, 31),
            fee_gross=Decimal("20.00"),
            fee_net=Decimal("16.39"),
            fee_vat=Decimal("3.61"),
            fee_vat_rate=Decimal("22.00"),
            org_name=invoice_org_no_billing_email.name,
            org_vat_id="",
            platform_business_name="Revel",
            platform_business_address="Test",
            platform_vat_id="IT99999999999",
            status=PlatformFeeInvoice.InvoiceStatus.ISSUED,
            issued_at=timezone.now(),
        )
        from django.core.files.base import ContentFile

        invoice.pdf_file.save("test.pdf", ContentFile(b"%PDF-fake"), save=True)

        send_invoice_email_task(str(invoice.id))

        call_kwargs = mock_send_email.call_args.kwargs
        recipients = call_kwargs["to"]
        assert "contact@nobilling.com" in recipients
        assert "invoice_owner@example.com" in recipients

    @patch("events.tasks.send_email")
    def test_does_not_duplicate_owner_email_in_recipients(
        self,
        mock_send_email: MagicMock,
        site_settings: SiteSettings,
        django_user_model: type[RevelUser],
    ) -> None:
        """When billing_email matches owner email, it appears only once in recipients."""
        owner = django_user_model.objects.create_user(
            username="dedup_owner",
            email="same@example.com",
            password="pass",
        )
        org = Organization.objects.create(
            name="Dedup Org",
            slug="dedup-org",
            owner=owner,
            billing_email="same@example.com",
        )
        invoice = PlatformFeeInvoice.objects.create(
            organization=org,
            invoice_number="RVL-2026-000055",
            period_start=date(2026, 4, 1),
            period_end=date(2026, 4, 30),
            fee_gross=Decimal("30.00"),
            fee_net=Decimal("24.59"),
            fee_vat=Decimal("5.41"),
            fee_vat_rate=Decimal("22.00"),
            org_name=org.name,
            org_vat_id="",
            platform_business_name="Revel",
            platform_business_address="Test",
            platform_vat_id="IT99999999999",
            status=PlatformFeeInvoice.InvoiceStatus.ISSUED,
            issued_at=timezone.now(),
        )
        from django.core.files.base import ContentFile

        invoice.pdf_file.save("test.pdf", ContentFile(b"%PDF-fake"), save=True)

        send_invoice_email_task(str(invoice.id))

        call_kwargs = mock_send_email.call_args.kwargs
        recipients = call_kwargs["to"]
        assert recipients.count("same@example.com") == 1

    def test_missing_invoice_raises_does_not_exist(
        self,
        site_settings: SiteSettings,
    ) -> None:
        """Requesting a non-existent invoice ID raises DoesNotExist (for Celery retry)."""
        fake_id = str(uuid4())

        with pytest.raises(PlatformFeeInvoice.DoesNotExist):
            send_invoice_email_task(fake_id)


# ===========================================================================
# revalidate_vat_ids_task
# ===========================================================================


class TestRevalidateVatIdsTask:
    """Tests for the revalidate_vat_ids_task Celery task."""

    @patch("events.tasks.revalidate_single_vat_id_task.delay")
    def test_dispatches_tasks_for_all_orgs_with_vat_ids(
        self,
        mock_single_delay: MagicMock,
        invoice_org: Organization,
    ) -> None:
        """A per-org revalidation task is dispatched for each org that has a VAT ID."""
        result = revalidate_vat_ids_task()

        mock_single_delay.assert_called_once_with(str(invoice_org.id))
        assert result == {"dispatched": 1}

    @patch("events.tasks.revalidate_single_vat_id_task.delay")
    def test_skips_orgs_without_vat_ids(
        self,
        mock_single_delay: MagicMock,
        invoice_owner: RevelUser,
    ) -> None:
        """Organizations with empty VAT IDs are not dispatched for revalidation."""
        Organization.objects.create(
            name="No VAT Org for Revalidation",
            slug="no-vat-org-reval",
            owner=invoice_owner,
            vat_id="",
        )

        result = revalidate_vat_ids_task()

        mock_single_delay.assert_not_called()
        assert result == {"dispatched": 0}

    @patch("events.tasks.revalidate_single_vat_id_task.delay")
    def test_dispatches_multiple_orgs(
        self,
        mock_single_delay: MagicMock,
        invoice_org: Organization,
        invoice_owner: RevelUser,
    ) -> None:
        """Multiple orgs with VAT IDs each get their own task dispatched."""
        org2 = Organization.objects.create(
            name="Second VAT Org",
            slug="second-vat-org",
            owner=invoice_owner,
            vat_id="DE987654321",
        )

        result = revalidate_vat_ids_task()

        dispatched_ids = {call.args[0] for call in mock_single_delay.call_args_list}
        assert str(invoice_org.id) in dispatched_ids
        assert str(org2.id) in dispatched_ids
        assert result == {"dispatched": 2}

    @patch("events.tasks.revalidate_single_vat_id_task.delay")
    def test_returns_count_of_dispatched_tasks(
        self,
        mock_single_delay: MagicMock,
    ) -> None:
        """When no orgs have VAT IDs, the dispatched count is zero."""
        result = revalidate_vat_ids_task()

        assert result == {"dispatched": 0}
        mock_single_delay.assert_not_called()


# ===========================================================================
# revalidate_single_vat_id_task
# ===========================================================================


class TestRevalidateSingleVatIdTask:
    """Tests for the revalidate_single_vat_id_task Celery task."""

    @patch("common.service.vies_service.httpx.post")
    def test_validates_and_updates_organization(
        self,
        mock_post: MagicMock,
        invoice_org: Organization,
    ) -> None:
        """The task calls validate_and_update_organization and updates the DB."""
        response = MagicMock()
        response.status_code = 200
        response.json.return_value = {
            "valid": True,
            "name": "Invoice Test Org",
            "address": "VIA ROMA 1",
            "requestIdentifier": "REVAL-001",
        }
        mock_post.return_value = response

        revalidate_single_vat_id_task(str(invoice_org.id))

        invoice_org.refresh_from_db()
        assert invoice_org.vat_id_validated is True
        assert invoice_org.vies_request_identifier == "REVAL-001"

    @patch("common.service.vies_service.httpx.post")
    def test_skips_org_without_vat_id(
        self,
        mock_post: MagicMock,
        invoice_owner: RevelUser,
    ) -> None:
        """If the org's VAT ID was cleared between dispatch and execution, the task is a no-op."""
        org = Organization.objects.create(
            name="Cleared VAT Org",
            slug="cleared-vat-org",
            owner=invoice_owner,
            vat_id="",
        )

        # Should return without calling VIES
        revalidate_single_vat_id_task(str(org.id))

        mock_post.assert_not_called()

    def test_missing_org_raises_does_not_exist(self) -> None:
        """Requesting a non-existent org ID raises DoesNotExist (for Celery retry)."""
        fake_id = str(uuid4())

        with pytest.raises(Organization.DoesNotExist):
            revalidate_single_vat_id_task(fake_id)

    @patch("common.service.vies_service.httpx.post")
    def test_vies_unavailable_propagates_for_retry(
        self,
        mock_post: MagicMock,
        invoice_org: Organization,
    ) -> None:
        """VIESUnavailableError propagates so Celery's autoretry can handle it.

        The task is configured with autoretry_for=(Exception,), so letting
        VIESUnavailableError bubble up triggers the retry mechanism.
        """
        import httpx

        mock_post.side_effect = httpx.ConnectError("Connection refused")

        from common.service.vies_service import VIESUnavailableError

        with pytest.raises(VIESUnavailableError):
            revalidate_single_vat_id_task(str(invoice_org.id))

    @patch("common.service.vies_service.httpx.post")
    def test_invalid_vat_id_sets_validated_false(
        self,
        mock_post: MagicMock,
        invoice_org: Organization,
    ) -> None:
        """When VIES reports the VAT ID as invalid, the org is updated accordingly."""
        response = MagicMock()
        response.status_code = 200
        response.json.return_value = {
            "valid": False,
            "name": "",
            "address": "",
            "requestIdentifier": "REVAL-INVALID",
        }
        mock_post.return_value = response

        revalidate_single_vat_id_task(str(invoice_org.id))

        invoice_org.refresh_from_db()
        assert invoice_org.vat_id_validated is False
        assert invoice_org.vies_request_identifier == "REVAL-INVALID"
