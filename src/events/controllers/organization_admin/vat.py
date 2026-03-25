"""Organization VAT settings, platform fee invoices, and attendee invoice endpoints."""

from uuid import UUID

from django.db.models import QuerySet
from django.shortcuts import get_object_or_404
from django.utils.translation import gettext_lazy as _
from ninja.errors import HttpError
from ninja_extra import api_controller, route
from ninja_extra.pagination import PageNumberPaginationExtra, PaginatedResponseSchema, paginate

from common.authentication import I18nJWTAuth
from common.signing import get_file_url
from common.throttling import UserDefaultThrottle, WriteThrottle
from events import models, schema
from events.controllers.permissions import IsOrganizationOwner
from events.service import vies_service

from .base import OrganizationAdminBaseController


@api_controller(
    "/organization-admin/{slug}",
    auth=I18nJWTAuth(),
    tags=["Organization Admin - VAT"],
    throttle=UserDefaultThrottle(),
)
class OrganizationAdminVATController(OrganizationAdminBaseController):
    """VAT settings and platform fee invoice management."""

    # ---- Billing Info ----

    @route.get(
        "/billing-info",
        url_name="get_billing_info",
        response=schema.OrganizationBillingInfoSchema,
        permissions=[IsOrganizationOwner()],
    )
    def get_billing_info(self, slug: str) -> models.Organization:
        """Get organization billing info and VAT settings."""
        return self.get_one(slug)

    @route.patch(
        "/billing-info",
        url_name="update_billing_info",
        response=schema.OrganizationBillingInfoSchema,
        permissions=[IsOrganizationOwner()],
        throttle=WriteThrottle(),
    )
    def update_billing_info(
        self, slug: str, payload: schema.OrganizationBillingInfoUpdateSchema
    ) -> models.Organization:
        """Update organization billing info."""
        organization = self.get_one(slug)
        update_data = payload.model_dump(exclude_unset=True)
        if not update_data:
            return organization
        vies_service.update_org_billing_info(organization, update_data)
        return organization

    @route.put(
        "/vat-id",
        url_name="set_vat_id",
        response=schema.OrganizationBillingInfoSchema,
        permissions=[IsOrganizationOwner()],
        throttle=WriteThrottle(),
    )
    def set_vat_id(self, slug: str, payload: schema.VATIdUpdateSchema) -> models.Organization:
        """Set or update the organization's VAT ID with VIES validation."""
        organization = self.get_one(slug)
        vies_service.set_org_vat_id(organization, payload.vat_id)
        return organization

    @route.delete(
        "/vat-id",
        url_name="delete_vat_id",
        response={204: None},
        permissions=[IsOrganizationOwner()],
        throttle=WriteThrottle(),
    )
    def delete_vat_id(self, slug: str) -> tuple[int, None]:
        """Clear the organization's VAT ID, country code, and validation status."""
        vies_service.clear_org_vat_fields(self.get_one(slug))
        return 204, None

    # ---- Platform Fee Invoices ----

    @route.get(
        "/invoices",
        url_name="list_invoices",
        response=PaginatedResponseSchema[schema.PlatformFeeInvoiceSchema],
        permissions=[IsOrganizationOwner()],
    )
    @paginate(PageNumberPaginationExtra, page_size=20)
    def list_invoices(self, slug: str) -> QuerySet[models.PlatformFeeInvoice]:
        """List platform fee invoices for this organization."""
        organization = self.get_one(slug)
        return models.PlatformFeeInvoice.objects.filter(organization=organization).order_by("-period_start")

    @route.get(
        "/invoices/{invoice_id}",
        url_name="get_invoice",
        response=schema.PlatformFeeInvoiceSchema,
        permissions=[IsOrganizationOwner()],
    )
    def get_invoice(self, slug: str, invoice_id: UUID) -> models.PlatformFeeInvoice:
        """Get a specific platform fee invoice."""
        organization = self.get_one(slug)
        return get_object_or_404(models.PlatformFeeInvoice, id=invoice_id, organization=organization)

    @route.get(
        "/invoices/{invoice_id}/download",
        url_name="download_invoice",
        response=schema.InvoiceDownloadURLSchema,
        permissions=[IsOrganizationOwner()],
    )
    def download_invoice(self, slug: str, invoice_id: UUID) -> schema.InvoiceDownloadURLSchema:
        """Get a signed download URL for an invoice PDF."""
        organization = self.get_one(slug)
        invoice = get_object_or_404(models.PlatformFeeInvoice, id=invoice_id, organization=organization)
        url = get_file_url(invoice.pdf_file)
        if not url:
            raise HttpError(404, str(_("Invoice PDF not yet generated.")))
        return schema.InvoiceDownloadURLSchema(download_url=url)

    # ---- Credit Notes ----

    @route.get(
        "/credit-notes",
        url_name="list_credit_notes",
        response=PaginatedResponseSchema[schema.PlatformFeeCreditNoteSchema],
        permissions=[IsOrganizationOwner()],
    )
    @paginate(PageNumberPaginationExtra, page_size=20)
    def list_credit_notes(self, slug: str) -> QuerySet[models.PlatformFeeCreditNote]:
        """List credit notes for this organization."""
        organization = self.get_one(slug)
        return models.PlatformFeeCreditNote.objects.filter(invoice__organization=organization).order_by("-created_at")

    # ---- Invoicing Mode ----

    @route.patch(
        "/invoicing",
        url_name="set_invoicing_mode",
        response=schema.OrganizationBillingInfoSchema,
        permissions=[IsOrganizationOwner()],
        throttle=WriteThrottle(),
    )
    def set_invoicing_mode(self, slug: str, payload: schema.InvoicingModeUpdateSchema) -> models.Organization:
        """Set the attendee invoicing mode for this organization.

        Modes:
        - `none`: No invoicing (default)
        - `hybrid`: Generate invoices as drafts for manual review and sending
        - `auto`: Generate and send invoices automatically on payment
        """
        from events.service.attendee_invoice_service import set_invoicing_mode

        organization = self.get_one(slug)
        return set_invoicing_mode(organization, payload.mode)

    # ---- Attendee Invoices ----

    @route.get(
        "/attendee-invoices",
        url_name="list_attendee_invoices",
        response=PaginatedResponseSchema[schema.AttendeeInvoiceDetailSchema],
        permissions=[IsOrganizationOwner()],
    )
    @paginate(PageNumberPaginationExtra, page_size=20)
    def list_attendee_invoices(self, slug: str) -> QuerySet[models.AttendeeInvoice]:
        """List attendee invoices issued by this organization."""
        organization = self.get_one(slug)
        return models.AttendeeInvoice.objects.filter(organization=organization).order_by("-created_at")

    @route.get(
        "/attendee-invoices/{invoice_id}",
        url_name="get_attendee_invoice",
        response=schema.AttendeeInvoiceDetailSchema,
        permissions=[IsOrganizationOwner()],
    )
    def get_attendee_invoice(self, slug: str, invoice_id: UUID) -> models.AttendeeInvoice:
        """Get a specific attendee invoice."""
        organization = self.get_one(slug)
        return get_object_or_404(models.AttendeeInvoice, id=invoice_id, organization=organization)

    @route.get(
        "/attendee-invoices/{invoice_id}/download",
        url_name="download_attendee_invoice",
        response=schema.InvoiceDownloadURLSchema,
        permissions=[IsOrganizationOwner()],
    )
    def download_attendee_invoice(self, slug: str, invoice_id: UUID) -> schema.InvoiceDownloadURLSchema:
        """Get a signed download URL for an attendee invoice PDF.

        Generates the PDF on-demand if not yet generated or invalidated by edits.
        """
        from events.service.attendee_invoice_service import ensure_pdf_exists

        organization = self.get_one(slug)
        invoice = get_object_or_404(models.AttendeeInvoice, id=invoice_id, organization=organization)
        ensure_pdf_exists(invoice)
        url = get_file_url(invoice.pdf_file)
        if not url:
            raise HttpError(404, str(_("Invoice PDF could not be generated.")))
        return schema.InvoiceDownloadURLSchema(download_url=url)

    @route.patch(
        "/attendee-invoices/{invoice_id}",
        url_name="update_attendee_invoice",
        response=schema.AttendeeInvoiceDetailSchema,
        permissions=[IsOrganizationOwner()],
        throttle=WriteThrottle(),
    )
    def update_attendee_invoice(
        self, slug: str, invoice_id: UUID, payload: schema.UpdateAttendeeInvoiceSchema
    ) -> models.AttendeeInvoice:
        """Edit a draft attendee invoice. Only DRAFT invoices can be edited."""
        from events.service.attendee_invoice_service import update_draft_invoice

        organization = self.get_one(slug)
        invoice = get_object_or_404(models.AttendeeInvoice, id=invoice_id, organization=organization)
        update_data = payload.model_dump(exclude_unset=True)
        return update_draft_invoice(invoice, update_data)

    @route.post(
        "/attendee-invoices/{invoice_id}/issue",
        url_name="issue_attendee_invoice",
        response=schema.AttendeeInvoiceDetailSchema,
        permissions=[IsOrganizationOwner()],
        throttle=WriteThrottle(),
    )
    def issue_attendee_invoice(self, slug: str, invoice_id: UUID) -> models.AttendeeInvoice:
        """Issue a draft attendee invoice and send it to the buyer."""
        from events.service.attendee_invoice_service import deliver_attendee_invoice, issue_draft_invoice

        organization = self.get_one(slug)
        invoice = get_object_or_404(models.AttendeeInvoice, id=invoice_id, organization=organization)
        invoice = issue_draft_invoice(invoice)
        deliver_attendee_invoice(invoice)
        return invoice

    @route.delete(
        "/attendee-invoices/{invoice_id}",
        url_name="delete_attendee_invoice",
        response={204: None},
        permissions=[IsOrganizationOwner()],
        throttle=WriteThrottle(),
    )
    def delete_attendee_invoice(self, slug: str, invoice_id: UUID) -> tuple[int, None]:
        """Delete a draft attendee invoice."""
        from events.service.attendee_invoice_service import delete_draft_invoice

        organization = self.get_one(slug)
        invoice = get_object_or_404(models.AttendeeInvoice, id=invoice_id, organization=organization)
        delete_draft_invoice(invoice)
        return 204, None

    # ---- Attendee Credit Notes ----

    @route.get(
        "/attendee-credit-notes",
        url_name="list_attendee_credit_notes",
        response=PaginatedResponseSchema[schema.AttendeeInvoiceCreditNoteSchema],
        permissions=[IsOrganizationOwner()],
    )
    @paginate(PageNumberPaginationExtra, page_size=20)
    def list_attendee_credit_notes(self, slug: str) -> QuerySet[models.AttendeeInvoiceCreditNote]:
        """List attendee invoice credit notes for this organization."""
        organization = self.get_one(slug)
        return (
            models.AttendeeInvoiceCreditNote.objects.filter(invoice__organization=organization)
            .select_related("invoice")
            .order_by("-created_at")
        )
