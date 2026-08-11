"""Wallet pass controllers for downloading passes."""

import time
import typing as t
from uuid import UUID

from django.db.models import QuerySet
from django.http import HttpResponse, HttpResponseRedirect
from ninja import Query
from ninja.errors import HttpError
from ninja_extra import api_controller, route

from common.authentication import I18nJWTAuth
from common.controllers import UserAwareController
from common.signing import get_file_url, verify_signature
from events.models import OrganizationMember, Ticket
from events.service import ticket_file_service
from events.service.ticket_file_service import get_apple_pass_generator
from events.utils import create_membership_pdf
from wallet.google import service as google_wallet_service
from wallet.schema import GoogleWalletSaveUrlSchema


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
        "/{ticket_id}/wallet/google",
        url_name="ticket_google_wallet_pass",
        summary="Add ticket to Google Wallet",
        description="Redirects to a signed 'save to Google Wallet' link for a ticket. "
        "Pass ?format=json to receive the link as JSON instead — browser clients cannot "
        "follow the cross-origin redirect.",
        response={200: GoogleWalletSaveUrlSchema, 302: None, 404: None, 503: None},
    )
    def google_wallet_save_link(
        self,
        ticket_id: UUID,
        format: t.Annotated[t.Literal["json"] | None, Query()] = None,
    ) -> HttpResponse | tuple[int, GoogleWalletSaveUrlSchema]:
        """Redirect to (or return as JSON) the Google Wallet save link for a ticket.

        The user must own the ticket. Unlike the Apple rail there is no file:
        the pass is created by Google when the user opens the save link.
        """
        ticket = self.get_object_or_exception(self.get_queryset(), id=ticket_id)

        if not ticket.google_pass_available:
            raise HttpError(503, "Google Wallet is not configured")

        save_url = google_wallet_service.ticket_save_url(ticket)
        if format == "json":
            return 200, GoogleWalletSaveUrlSchema(save_url=save_url)
        return HttpResponseRedirect(save_url)

    @route.get(
        "/{ticket_id}/wallet/apple/signed",
        url_name="ticket_apple_wallet_signed",
        summary="Download Apple Wallet pass via signed link",
        description="Auth-free pkpass download guarded by an HMAC signature and expiry; "
        "used by the Add to Apple Wallet badge in ticket emails.",
        response={200: None, 403: None, 404: None, 410: None, 503: None},
        auth=None,
    )
    def download_apple_pass_signed(
        self,
        ticket_id: UUID,
        exp: t.Annotated[str, Query()],
        sig: t.Annotated[str, Query()],
    ) -> HttpResponse:
        """Serve a ticket's pkpass from a signed email link.

        Capability URL: possession of a valid signature authorizes the download
        (same exposure as the .pkpass attached to the same email). The signature
        expires when the pass itself would (event end + grace).
        """
        try:
            expires = int(exp)
        except ValueError:
            raise HttpError(403, "Invalid link")
        if expires <= time.time():
            raise HttpError(410, "Link expired")

        request_path = self.context.request.path  # type: ignore[union-attr]
        if not verify_signature(request_path, exp, sig):
            raise HttpError(403, "Invalid link")

        ticket = self.get_object_or_exception(
            Ticket.objects.full().filter(status__in=[Ticket.TicketStatus.ACTIVE, Ticket.TicketStatus.PENDING]),
            id=ticket_id,
        )

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


@api_controller("/me/organizations/{slug}/membership", tags=["Membership - Wallet"], auth=I18nJWTAuth())
class MembershipWalletController(UserAwareController):
    """Member-facing membership card downloads (Apple / Google / PDF)."""

    def get_member(self, slug: str) -> OrganizationMember:
        """The caller's own membership in the org; CANCELLED/BANNED are a 404."""
        return self.get_object_or_exception(
            OrganizationMember.objects.for_visibility().select_related("organization", "tier", "user"),
            organization__slug=slug,
            user=self.user(),
        )

    @route.get(
        "/wallet/apple",
        url_name="me_membership_apple_wallet_pass",
        summary="Download Apple Wallet membership card",
        response={200: None, 404: None, 503: None},
    )
    def download_apple_pass(self, slug: str) -> HttpResponse:
        """Generate and download the caller's membership card (.pkpass)."""
        member = self.get_member(slug)
        if not member.apple_pass_available:
            raise HttpError(503, "Apple Wallet is not configured")
        pkpass_bytes = get_apple_pass_generator().generate_membership_pass(member)
        response = HttpResponse(pkpass_bytes, content_type="application/vnd.apple.pkpass")
        safe_name = "membership_" + str(member.id).split("-")[0]
        response["Content-Disposition"] = f'attachment; filename="{safe_name}.pkpass"'
        return response

    @route.get(
        "/wallet/google",
        url_name="me_membership_google_wallet_pass",
        summary="Add membership card to Google Wallet",
        response={200: GoogleWalletSaveUrlSchema, 302: None, 404: None, 503: None},
    )
    def google_wallet_save_link(
        self,
        slug: str,
        format: t.Annotated[t.Literal["json"] | None, Query()] = None,
    ) -> HttpResponse | tuple[int, GoogleWalletSaveUrlSchema]:
        """Redirect to (or return as JSON) the Google Wallet save link for the caller's card."""
        member = self.get_member(slug)
        if not member.google_pass_available:
            raise HttpError(503, "Google Wallet is not configured")
        save_url = google_wallet_service.membership_save_url(member)
        if format == "json":
            return 200, GoogleWalletSaveUrlSchema(save_url=save_url)
        return HttpResponseRedirect(save_url)

    @route.get(
        "/pdf",
        url_name="me_membership_pdf",
        summary="Download PDF membership card",
        response={200: None, 404: None},
    )
    def download_pdf(self, slug: str) -> HttpResponse:
        """Generate and download the caller's membership card as a PDF."""
        member = self.get_member(slug)
        # ponytail: generated per-request (rare downloads); if this gets hot, cache
        # like ticket_file_service.get_or_generate_pdf (needs a file field + migration).
        pdf_bytes = create_membership_pdf(member)
        response = HttpResponse(pdf_bytes, content_type="application/pdf")
        safe_name = "membership_" + str(member.id).split("-")[0]
        response["Content-Disposition"] = f'attachment; filename="{safe_name}.pdf"'
        return response


@api_controller("/memberships", tags=["Membership - Wallet"], auth=I18nJWTAuth())
class MembershipWalletSignedController(UserAwareController):
    """Signed (auth-free) membership card download for email badges."""

    @route.get(
        "/{member_id}/wallet/apple/signed",
        url_name="membership_apple_wallet_signed",
        summary="Download Apple Wallet membership card via signed link",
        description="Auth-free pkpass download guarded by an HMAC signature and expiry; "
        "used by the Add to Apple Wallet badge in membership emails.",
        response={200: None, 403: None, 404: None, 410: None, 503: None},
        auth=None,
    )
    def download_apple_pass_signed(
        self,
        member_id: UUID,
        exp: t.Annotated[str, Query()],
        sig: t.Annotated[str, Query()],
    ) -> HttpResponse:
        """Serve a membership pkpass from a signed email link (capability URL)."""
        try:
            expires = int(exp)
        except ValueError:
            raise HttpError(403, "Invalid link")
        if expires <= time.time():
            raise HttpError(410, "Link expired")
        request_path = self.context.request.path  # type: ignore[union-attr]
        if not verify_signature(request_path, exp, sig):
            raise HttpError(403, "Invalid link")

        member = self.get_object_or_exception(
            OrganizationMember.objects.for_visibility().select_related("organization", "tier", "user"),
            id=member_id,
        )
        if not member.apple_pass_available:
            raise HttpError(503, "Apple Wallet is not configured")
        pkpass_bytes = get_apple_pass_generator().generate_membership_pass(member)
        response = HttpResponse(pkpass_bytes, content_type="application/vnd.apple.pkpass")
        safe_name = "membership_" + str(member.id).split("-")[0]
        response["Content-Disposition"] = f'attachment; filename="{safe_name}.pkpass"'
        return response
