"""Wallet pass controllers for downloading passes."""

from uuid import UUID

from django.db.models import QuerySet
from django.http import HttpResponse
from ninja.errors import HttpError
from ninja_extra import api_controller, route

from common.authentication import I18nJWTAuth
from common.controllers import UserAwareController
from events.models import Ticket
from wallet.apple.generator import ApplePassGenerator

# Module-level cached generator instance
_apple_pass_generator: ApplePassGenerator | None = None


def get_apple_pass_generator() -> ApplePassGenerator:
    """Get or create the cached Apple pass generator."""
    global _apple_pass_generator
    if _apple_pass_generator is None:
        _apple_pass_generator = ApplePassGenerator()
    return _apple_pass_generator


@api_controller("/tickets", tags=["Tickets - Wallet"], auth=I18nJWTAuth())
class TicketWalletController(UserAwareController):
    """Controller for downloading wallet passes for tickets."""

    def get_queryset(self) -> QuerySet[Ticket]:
        """Get tickets owned by the current user."""
        return Ticket.objects.filter(
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
        """
        ticket = self.get_object_or_exception(self.get_queryset(), id=ticket_id)

        if not ticket.apple_pass_available:
            raise HttpError(503, "Apple Wallet is not configured")

        generator = get_apple_pass_generator()
        pkpass = generator.generate_pass(ticket)

        response = HttpResponse(pkpass, content_type=generator.CONTENT_TYPE)
        safe_name = "event_" + str(ticket.id).split("-")[0]
        response["Content-Disposition"] = f'attachment; filename="{safe_name}.pkpass"'
        return response
