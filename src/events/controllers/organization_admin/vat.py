"""Organization VAT settings and platform fee invoice endpoints."""

from uuid import UUID

import structlog
from django.db.models import QuerySet
from django.shortcuts import get_object_or_404
from django.utils.translation import gettext_lazy as _
from ninja.errors import HttpError
from ninja_extra import api_controller, route
from ninja_extra.pagination import PageNumberPaginationExtra, PaginatedResponseSchema, paginate

from common.authentication import I18nJWTAuth
from common.service.vies_service import VIESUnavailableError
from common.signing import get_file_url
from common.throttling import UserDefaultThrottle, WriteThrottle
from events import models, schema
from events.controllers.permissions import IsOrganizationOwner
from events.service.vies_service import validate_and_update_organization

from .base import OrganizationAdminBaseController

logger = structlog.get_logger(__name__)


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
        """Update organization billing info (country code, VAT rate, billing address).

        VAT ID is managed separately via PUT/DELETE /vat-id.
        If a VAT ID exists, vat_country_code must match its prefix.
        """
        organization = self.get_one(slug)
        update_data = payload.model_dump(exclude_unset=True)
        if not update_data:
            return organization

        # Reject country code changes that conflict with the VAT ID prefix
        new_country = update_data.get("vat_country_code")
        if new_country and organization.vat_id:
            vat_prefix = organization.vat_id[:2].upper()
            if new_country != vat_prefix:
                raise HttpError(
                    400,
                    str(_("Country code must match the VAT ID prefix (%(prefix)s).") % {"prefix": vat_prefix}),
                )

        for field, value in update_data.items():
            setattr(organization, field, value)
        organization.save(update_fields=[*update_data.keys(), "updated_at"])
        return organization

    @route.put(
        "/vat-id",
        url_name="set_vat_id",
        response=schema.OrganizationBillingInfoSchema,
        permissions=[IsOrganizationOwner()],
        throttle=WriteThrottle(),
    )
    def set_vat_id(self, slug: str, payload: schema.VATIdUpdateSchema) -> models.Organization:
        """Set or update the organization's VAT ID.

        Validates format via regex, saves the VAT ID, then triggers VIES validation
        synchronously. On successful VIES validation, auto-fills billing_address and
        vat_country_code from the VIES response if they are currently empty.
        """
        organization = self.get_one(slug)

        # Save the new VAT ID with reset validation status.
        # Always set vat_country_code from the VAT ID prefix to keep them in sync.
        organization.vat_id = payload.vat_id
        organization.vat_country_code = payload.vat_id[:2].upper()
        organization.vat_id_validated = False
        organization.vat_id_validated_at = None
        organization.save(
            update_fields=["vat_id", "vat_country_code", "vat_id_validated", "vat_id_validated_at", "updated_at"]
        )

        # Trigger VIES validation
        try:
            result = validate_and_update_organization(organization)
            if not result.valid:
                raise HttpError(400, str(_("The VAT ID is not valid according to VIES.")))
        except VIESUnavailableError:
            logger.warning("vies_unavailable", org_id=str(organization.id))
            from events.tasks import revalidate_single_vat_id_task

            revalidate_single_vat_id_task.delay(str(organization.id))
            raise HttpError(
                503,
                str(
                    _(
                        "VIES validation service is temporarily unavailable."
                        " The VAT ID has been saved and will be validated automatically."
                    )
                ),
            )

        organization.refresh_from_db()
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
        organization = self.get_one(slug)
        organization.vat_id = ""
        organization.vat_country_code = ""
        organization.vat_id_validated = False
        organization.vat_id_validated_at = None
        organization.vies_request_identifier = ""
        organization.save(
            update_fields=[
                "vat_id",
                "vat_country_code",
                "vat_id_validated",
                "vat_id_validated_at",
                "vies_request_identifier",
                "updated_at",
            ]
        )
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
