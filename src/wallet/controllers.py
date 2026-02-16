"""Wallet pass controllers for downloading passes."""

from uuid import UUID

from django.db.models import QuerySet
from django.http import HttpResponse, HttpResponseRedirect
from ninja.errors import HttpError
from ninja_extra import api_controller, route

from common.authentication import I18nJWTAuth
from common.controllers import UserAwareController
from common.signing import get_file_url
from events.models import Ticket
from events.service import ticket_file_service


@api_controller("/tickets", tags=["Tickets - Wallet"], auth=I18nJWTAuth())
class TicketWalletController(UserAwareController):
    """Controller for downloading wallet passes and ticket files."""

    def get_queryset(self) -> QuerySet[Ticket]:
        """Get tickets owned by the current user."""
        return Ticket.objects.full().filter(
            user=self.user(),
            status__in=[Ticket.TicketStatus.ACTIVE, Ticket.TicketStatus.PENDING],
        )

    @route.get(
        "/{ticket_id}/wallet/apple",
        url_name="ticket_apple_wallet_pass",
        summary="Download Apple Wallet pass",
        description="Generate and download an Apple Wallet pass (.pkpass) for a ticket.",
        response={200: None, 404: None, 503: None},
    )
    def download_apple_pass(self, ticket_id: UUID) -> HttpResponse:
        """Download an Apple Wallet pass for a ticket.

        The user must own the ticket to download its pass.

        Note: Unlike the PDF endpoint, pkpass files are always served as direct
        byte responses (not redirects to signed URLs). Apple Wallet clients
        do not reliably follow HTTP redirects when importing passes.
        """
        ticket = self.get_object_or_exception(self.get_queryset(), id=ticket_id)

        if not ticket.apple_pass_available:
            raise HttpError(503, "Apple Wallet is not configured")

        pkpass_bytes = ticket_file_service.get_or_generate_pkpass(ticket)

        response = HttpResponse(pkpass_bytes, content_type="application/vnd.apple.pkpass")
        safe_name = "ticket_" + str(ticket.id).split("-")[0]
        response["Content-Disposition"] = f'attachment; filename="{safe_name}.pkpass"'
        return response

    @route.get(
        "/{ticket_id}/pdf",
        url_name="ticket_pdf_download",
        summary="Download PDF ticket",
        description="Generate and download a PDF version of a ticket. "
        "Redirects to a signed URL served by Caddy when the file is cached.",
        response={200: None, 302: None, 404: None},
    )
    def download_pdf(self, ticket_id: UUID) -> HttpResponse:
        """Download a PDF version of a ticket.

        Ensures the PDF is cached, then redirects to a signed URL so Caddy
        serves the file directly. Falls back to serving bytes from Django
        if caching fails.
        """
        ticket = self.get_object_or_exception(self.get_queryset(), id=ticket_id)

        # Fast path: redirect to signed URL if cache is still valid
        if ticket_file_service.is_cache_valid(ticket) and (signed_url := get_file_url(ticket.pdf_file)):
            return HttpResponseRedirect(signed_url)

        # Cache miss or signed URL unavailable: generate and cache
        pdf_bytes = ticket_file_service.get_or_generate_pdf(ticket)

        # Refresh to pick up DB state written by _persist_and_update
        ticket.refresh_from_db()
        if signed_url := get_file_url(ticket.pdf_file):
            return HttpResponseRedirect(signed_url)

        # Fallback: serve directly if caching failed
        response = HttpResponse(pdf_bytes, content_type="application/pdf")
        safe_name = "ticket_" + str(ticket.id).split("-")[0]
        response["Content-Disposition"] = f'attachment; filename="{safe_name}.pdf"'
        return response
