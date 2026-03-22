"""Organization VAT settings and platform fee invoice endpoints."""

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
    def download_invoice(self, slug: str, invoice_id: UUID) -> dict[str, str]:
        """Get a signed download URL for an invoice PDF."""
        organization = self.get_one(slug)
        invoice = get_object_or_404(models.PlatformFeeInvoice, id=invoice_id, organization=organization)
        url = get_file_url(invoice.pdf_file)
        if not url:
            raise HttpError(404, str(_("Invoice PDF not yet generated.")))
        return {"download_url": url}

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
