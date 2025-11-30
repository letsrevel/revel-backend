"""Django Ninja controllers for wallet pass API endpoints.

This module provides two sets of endpoints:

1. Apple Wallet Web Service API (at /wallet/v1/...)
   - Device registration/unregistration
   - Pass retrieval and updates
   - Error logging
   These are called by Apple Wallet, not by our frontend.

2. User-facing API (at /api/...)
   - Download pass for a ticket
   These are called by our frontend for users to add passes to their wallets.
"""

import typing as t
from uuid import UUID

import structlog
from django.conf import settings
from django.http import HttpRequest, HttpResponse
from ninja import Router
from ninja_extra import api_controller, route
from ninja_extra.controllers.base import ControllerBase

from common.authentication import I18nJWTAuth, OptionalAuth
from events.models import Ticket
from wallet.schemas import DeviceRegistrationPayload, LogPayload, SerialNumbersResponse
from wallet.service import WalletService, WalletServiceError, get_wallet_service

logger = structlog.get_logger(__name__)

# Router for Apple Wallet web service callbacks (no auth - uses pass auth token)
apple_router = Router(tags=["Apple Wallet Web Service"])


def _get_auth_token(request: HttpRequest) -> str | None:
    """Extract authentication token from Authorization header.

    Apple sends: Authorization: ApplePass <authenticationToken>

    Args:
        request: The HTTP request.

    Returns:
        The auth token or None if not present/invalid.
    """
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("ApplePass "):
        return auth_header[10:]  # Remove "ApplePass " prefix
    return None


# -----------------------------------------------------------------------------
# Apple Wallet Web Service Endpoints
# These implement the Apple Passbook Web Service Reference
# https://developer.apple.com/library/archive/documentation/PassKit/Reference/PassKit_WebService/WebService.html
# -----------------------------------------------------------------------------


@apple_router.post(
    "/v1/devices/{device_library_id}/registrations/{pass_type_id}/{serial_number}",
    response={200: None, 201: None, 401: None},
    url_name="wallet_register_device",
)
def register_device(
    request: HttpRequest,
    device_library_id: str,
    pass_type_id: str,
    serial_number: str,
    payload: DeviceRegistrationPayload,
) -> HttpResponse:
    """Register a device to receive push notifications for a pass.

    Called by Apple Wallet when a pass is added to the wallet.

    Returns:
        200: Registration already exists
        201: Registration created successfully
        401: Invalid authorization
    """
    auth_token = _get_auth_token(request)
    if not auth_token:
        logger.warning("missing_auth_token", device=device_library_id[:20])
        return HttpResponse(status=401)

    # Verify pass type matches our configuration
    if pass_type_id != settings.APPLE_WALLET_PASS_TYPE_ID:
        logger.warning(
            "invalid_pass_type",
            expected=settings.APPLE_WALLET_PASS_TYPE_ID,
            received=pass_type_id,
        )
        return HttpResponse(status=401)

    try:
        service = get_wallet_service()
        created = service.register_device(
            device_library_id=device_library_id,
            push_token=payload.pushToken,
            serial_number=serial_number,
            auth_token=auth_token,
        )

        return HttpResponse(status=201 if created else 200)

    except WalletServiceError as e:
        logger.warning("device_registration_failed", error=str(e))
        return HttpResponse(status=401)


@apple_router.delete(
    "/v1/devices/{device_library_id}/registrations/{pass_type_id}/{serial_number}",
    response={200: None, 401: None},
    url_name="wallet_unregister_device",
)
def unregister_device(
    request: HttpRequest,
    device_library_id: str,
    pass_type_id: str,
    serial_number: str,
) -> HttpResponse:
    """Unregister a device from receiving updates for a pass.

    Called by Apple Wallet when a pass is removed from the wallet.

    Returns:
        200: Unregistration successful (or already unregistered)
        401: Invalid authorization
    """
    auth_token = _get_auth_token(request)
    if not auth_token:
        return HttpResponse(status=401)

    if pass_type_id != settings.APPLE_WALLET_PASS_TYPE_ID:
        return HttpResponse(status=401)

    service = get_wallet_service()
    service.unregister_device(
        device_library_id=device_library_id,
        serial_number=serial_number,
        auth_token=auth_token,
    )

    return HttpResponse(status=200)


@apple_router.get(
    "/v1/devices/{device_library_id}/registrations/{pass_type_id}",
    response={200: SerialNumbersResponse, 204: None},
    url_name="wallet_get_serial_numbers",
)
def get_serial_numbers(
    request: HttpRequest,
    device_library_id: str,
    pass_type_id: str,
    passesUpdatedSince: str | None = None,
) -> HttpResponse | SerialNumbersResponse:
    """Get serial numbers of passes that need updating.

    Called by device after receiving a push notification.

    Args:
        request: The HTTP request.
        device_library_id: Unique identifier for the device.
        pass_type_id: Pass type identifier (must match our configuration).
        passesUpdatedSince: Unix timestamp of last update (query param).

    Returns:
        200: JSON with serialNumbers array and lastUpdated timestamp
        204: No passes need updating
    """
    if pass_type_id != settings.APPLE_WALLET_PASS_TYPE_ID:
        return HttpResponse(status=204)

    service = get_wallet_service()
    serial_numbers, last_updated = service.get_updated_passes(
        device_library_id=device_library_id,
        pass_type_id=pass_type_id,
        passes_updated_since=passesUpdatedSince,
    )

    if not serial_numbers:
        return HttpResponse(status=204)

    return SerialNumbersResponse(
        serialNumbers=serial_numbers,
        lastUpdated=last_updated,
    )


@apple_router.get(
    "/v1/passes/{pass_type_id}/{serial_number}",
    url_name="wallet_get_pass",
)
def get_latest_pass(
    request: HttpRequest,
    pass_type_id: str,
    serial_number: str,
) -> HttpResponse:
    """Get the latest version of a pass.

    Called by device to download an updated pass.

    Returns:
        200: The .pkpass file
        304: Pass not modified since If-Modified-Since
        401: Invalid authorization
    """
    auth_token = _get_auth_token(request)
    if not auth_token:
        return HttpResponse(status=401)

    if pass_type_id != settings.APPLE_WALLET_PASS_TYPE_ID:
        return HttpResponse(status=401)

    if_modified_since = request.headers.get("If-Modified-Since")

    try:
        service = get_wallet_service()
        pkpass, last_modified = service.get_pass_for_device(
            serial_number=serial_number,
            auth_token=auth_token,
            if_modified_since=if_modified_since,
        )

        if pkpass is None:
            # Not modified
            response = HttpResponse(status=304)
            response["Last-Modified"] = last_modified
            return response

        response = HttpResponse(
            pkpass,
            content_type="application/vnd.apple.pkpass",
            status=200,
        )
        response["Last-Modified"] = last_modified
        return response

    except WalletServiceError as e:
        logger.warning("get_pass_failed", serial=serial_number, error=str(e))
        return HttpResponse(status=401)


@apple_router.post(
    "/v1/log",
    response={200: None},
    url_name="wallet_log",
)
def log_errors(request: HttpRequest, payload: LogPayload) -> HttpResponse:
    """Receive error logs from devices.

    Apple Wallet sends logs here when it encounters errors with passes.
    Useful for debugging pass issues.

    Returns:
        200: Always (logs are best-effort)
    """
    for log_message in payload.logs:
        logger.info("apple_wallet_device_log", message=log_message)

    return HttpResponse(status=200)


# -----------------------------------------------------------------------------
# User-facing API Endpoints
# -----------------------------------------------------------------------------


@api_controller("/tickets", tags=["Tickets - Wallet"], auth=I18nJWTAuth())
class TicketWalletController(ControllerBase):
    """Controller for user-facing wallet pass endpoints."""

    def __init__(self) -> None:
        """Initialize controller."""
        super().__init__()
        self._service: WalletService | None = None

    @property
    def service(self) -> WalletService:
        """Get wallet service instance."""
        if self._service is None:
            self._service = get_wallet_service()
        return self._service

    def user(self) -> t.Any:
        """Get current authenticated user."""
        return self.context.request.user  # type: ignore[union-attr]

    @route.get(
        "/{ticket_id}/wallet/apple",
        url_name="ticket_apple_wallet_pass",
        summary="Download Apple Wallet pass",
        description="Generate and download an Apple Wallet pass (.pkpass) for a ticket.",
        response={200: None, 404: None, 500: None, 503: None},
    )
    def download_apple_pass(self, ticket_id: UUID) -> HttpResponse:
        """Download an Apple Wallet pass for a ticket.

        The user must own the ticket to download its pass.

        Returns:
            200: The .pkpass file
            404: Ticket not found or not owned by user
            503: Apple Wallet not configured
        """
        user = self.user()

        # Get ticket and verify ownership
        ticket = Ticket.objects.filter(
            id=ticket_id,
            user=user,
            status__in=[Ticket.TicketStatus.ACTIVE, Ticket.TicketStatus.PENDING],
        ).first()

        if not ticket:
            return HttpResponse(status=404)

        if not self.service.is_apple_wallet_configured():
            return HttpResponse(
                "Apple Wallet is not configured",
                status=503,
                content_type="text/plain",
            )

        try:
            # Check if there's an existing registration with auth token
            from wallet.models import WalletPassRegistration

            existing_reg = WalletPassRegistration.objects.filter(ticket=ticket).first()
            auth_token = existing_reg.auth_token if existing_reg else None

            pkpass = self.service.generate_apple_pass(ticket, auth_token=auth_token)

            response = HttpResponse(
                pkpass,
                content_type="application/vnd.apple.pkpass",
            )

            # Set filename for download
            safe_event_name = ticket.event.name.replace(" ", "_")[:30]
            response["Content-Disposition"] = f'attachment; filename="{safe_event_name}.pkpass"'

            return response

        except WalletServiceError as e:
            logger.error("pass_download_failed", ticket_id=str(ticket_id), error=str(e))
            return HttpResponse(
                "Failed to generate pass",
                status=500,
                content_type="text/plain",
            )


@api_controller("/tickets", tags=["Tickets - Wallet"], auth=OptionalAuth())
class TicketWalletPublicController(ControllerBase):
    """Controller for public wallet pass endpoints (e.g., direct links)."""

    @route.get(
        "/{ticket_id}/wallet/apple/check",
        url_name="ticket_apple_wallet_check",
        summary="Check if Apple Wallet is available",
        description="Check if Apple Wallet passes can be generated.",
    )
    def check_apple_wallet_available(self, ticket_id: UUID) -> dict[str, bool]:
        """Check if Apple Wallet passes are available.

        Returns:
            JSON with 'available' boolean.
        """
        service = get_wallet_service()
        return {"available": service.is_apple_wallet_configured()}
