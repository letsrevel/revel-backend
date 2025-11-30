"""Wallet service for pass generation and management.

This module provides the main service layer for wallet pass operations,
orchestrating pass generation, device registration, and update notifications.
"""

from uuid import UUID

import structlog
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from events.models import Ticket
from wallet.apple.generator import ApplePassGenerator, ApplePassGeneratorError
from wallet.apple.push import ApplePushError, ApplePushNotificationClient
from wallet.apple.signer import ApplePassSigner
from wallet.models import (
    WalletPassDevice,
    WalletPassRegistration,
    WalletPassUpdateLog,
)

logger = structlog.get_logger(__name__)


class WalletServiceError(Exception):
    """Base exception for wallet service errors."""

    pass


class WalletService:
    """Service for managing wallet passes.

    This service provides a unified interface for:
    - Generating wallet passes for tickets
    - Registering/unregistering devices for pass updates
    - Sending update notifications when passes change
    - Querying updated passes for devices
    """

    def __init__(self) -> None:
        """Initialize the wallet service."""
        self._apple_generator: ApplePassGenerator | None = None
        self._apple_push_client: ApplePushNotificationClient | None = None

    @property
    def apple_generator(self) -> ApplePassGenerator:
        """Get the Apple pass generator, creating if needed."""
        if self._apple_generator is None:
            signer = ApplePassSigner()
            self._apple_generator = ApplePassGenerator(signer=signer)
        return self._apple_generator

    @property
    def apple_push_client(self) -> ApplePushNotificationClient:
        """Get the Apple push notification client, creating if needed."""
        if self._apple_push_client is None:
            self._apple_push_client = ApplePushNotificationClient()
        return self._apple_push_client

    def is_apple_wallet_configured(self) -> bool:
        """Check if Apple Wallet is properly configured.

        Returns:
            True if all required settings are present.
        """
        return bool(
            settings.APPLE_WALLET_PASS_TYPE_ID
            and settings.APPLE_WALLET_TEAM_ID
            and settings.APPLE_WALLET_CERT_PATH
            and settings.APPLE_WALLET_KEY_PATH
            and settings.APPLE_WALLET_WWDR_CERT_PATH
        )

    # -------------------------------------------------------------------------
    # Pass Generation
    # -------------------------------------------------------------------------

    def generate_apple_pass(self, ticket: Ticket, auth_token: str | None = None) -> bytes:
        """Generate an Apple Wallet pass for a ticket.

        Args:
            ticket: The ticket to generate a pass for.
            auth_token: Optional pre-generated auth token.

        Returns:
            The .pkpass file as bytes.

        Raises:
            WalletServiceError: If pass generation fails.
        """
        if not self.is_apple_wallet_configured():
            raise WalletServiceError("Apple Wallet is not configured")

        try:
            pkpass = self.apple_generator.generate_pass(ticket, auth_token)

            # Log the generation
            WalletPassUpdateLog.objects.create(
                ticket=ticket,
                update_type=WalletPassUpdateLog.UpdateType.PASS_GENERATED,
                details={"size": len(pkpass)},
            )

            return pkpass

        except ApplePassGeneratorError as e:
            logger.error("apple_pass_generation_failed", ticket_id=str(ticket.id), error=str(e))
            raise WalletServiceError(f"Failed to generate Apple pass: {e}")

    def get_pass_for_device(
        self,
        serial_number: str,
        auth_token: str,
        if_modified_since: str | None = None,
    ) -> tuple[bytes | None, str]:
        """Get a pass for a device callback request.

        This is called by Apple when a device needs an updated pass.

        Args:
            serial_number: The pass serial number (ticket UUID).
            auth_token: The authentication token from the request.
            if_modified_since: HTTP If-Modified-Since header value.

        Returns:
            Tuple of (pass_bytes or None if not modified, last_modified timestamp).

        Raises:
            WalletServiceError: If the pass cannot be retrieved.
        """
        try:
            ticket_id = UUID(serial_number)
        except ValueError:
            raise WalletServiceError(f"Invalid serial number: {serial_number}")

        # Find the registration to validate auth token
        registration = WalletPassRegistration.objects.filter(
            ticket_id=ticket_id,
            auth_token=auth_token,
        ).first()

        if not registration:
            raise WalletServiceError("Invalid authentication token")

        ticket = registration.ticket

        # Check if-modified-since
        last_modified = ticket.updated_at.strftime("%a, %d %b %Y %H:%M:%S GMT")

        if if_modified_since:
            # Parse and compare timestamps
            try:
                from email.utils import parsedate_to_datetime

                client_time = parsedate_to_datetime(if_modified_since)
                if client_time >= ticket.updated_at:
                    return None, last_modified
            except Exception:
                pass  # If parsing fails, return the pass anyway

        # Generate fresh pass
        pkpass = self.generate_apple_pass(ticket, auth_token=registration.auth_token)

        # Log the fetch
        WalletPassUpdateLog.objects.create(
            ticket=ticket,
            device=registration.device,
            update_type=WalletPassUpdateLog.UpdateType.PASS_FETCHED,
        )

        return pkpass, last_modified

    # -------------------------------------------------------------------------
    # Device Registration
    # -------------------------------------------------------------------------

    def register_device(
        self,
        device_library_id: str,
        push_token: str,
        serial_number: str,
        auth_token: str,
        platform: str = WalletPassDevice.Platform.APPLE,
    ) -> bool:
        """Register a device to receive pass updates.

        Called when a pass is added to a user's wallet.

        Args:
            device_library_id: Unique device identifier from wallet app.
            push_token: Token for sending push notifications to this device.
            serial_number: Pass serial number (ticket UUID).
            auth_token: Authentication token from the pass.
            platform: Wallet platform (apple/google).

        Returns:
            True if registration was created, False if already existed.

        Raises:
            WalletServiceError: If registration fails.
        """
        try:
            ticket_id = UUID(serial_number)
        except ValueError:
            raise WalletServiceError(f"Invalid serial number: {serial_number}")

        # Verify the ticket exists
        try:
            ticket = Ticket.objects.get(id=ticket_id)
        except Ticket.DoesNotExist:
            raise WalletServiceError(f"Ticket not found: {serial_number}")

        with transaction.atomic():
            # Get or create the device
            device, _ = WalletPassDevice.objects.update_or_create(
                device_library_id=device_library_id,
                defaults={
                    "push_token": push_token,
                    "platform": platform,
                },
            )

            # Create registration if it doesn't exist
            registration, created = WalletPassRegistration.objects.get_or_create(
                ticket=ticket,
                device=device,
                defaults={"auth_token": auth_token},
            )

            if created:
                WalletPassUpdateLog.objects.create(
                    ticket=ticket,
                    device=device,
                    update_type=WalletPassUpdateLog.UpdateType.DEVICE_REGISTERED,
                    details={
                        "device_library_id": device_library_id[:20],
                    },
                )

                logger.info(
                    "device_registered",
                    ticket_id=str(ticket_id),
                    device_id=device_library_id[:20],
                )

            return created

    def unregister_device(
        self,
        device_library_id: str,
        serial_number: str,
        auth_token: str,
    ) -> bool:
        """Unregister a device from receiving pass updates.

        Called when a pass is removed from a user's wallet.

        Args:
            device_library_id: Unique device identifier.
            serial_number: Pass serial number (ticket UUID).
            auth_token: Authentication token from the request.

        Returns:
            True if registration was removed, False if not found.
        """
        try:
            ticket_id = UUID(serial_number)
        except ValueError:
            return False

        deleted_count, _ = WalletPassRegistration.objects.filter(
            ticket_id=ticket_id,
            device__device_library_id=device_library_id,
            auth_token=auth_token,
        ).delete()

        if deleted_count > 0:
            # Log the unregistration
            WalletPassUpdateLog.objects.create(
                ticket_id=ticket_id,
                update_type=WalletPassUpdateLog.UpdateType.DEVICE_UNREGISTERED,
                details={"device_library_id": device_library_id[:20]},
            )

            logger.info(
                "device_unregistered",
                ticket_id=str(ticket_id),
                device_id=device_library_id[:20],
            )

        return deleted_count > 0

    # -------------------------------------------------------------------------
    # Pass Updates
    # -------------------------------------------------------------------------

    def get_updated_passes(
        self,
        device_library_id: str,
        pass_type_id: str,
        passes_updated_since: str | None = None,
    ) -> tuple[list[str], str]:
        """Get list of passes that have been updated since a given time.

        Called by device to check which passes need refreshing.

        Args:
            device_library_id: The device requesting updates.
            pass_type_id: The pass type identifier (must match our config).
            passes_updated_since: Unix timestamp string of last update.

        Returns:
            Tuple of (list of serial numbers, last_updated timestamp).
        """
        # Verify pass type matches our configuration
        if pass_type_id != settings.APPLE_WALLET_PASS_TYPE_ID:
            return [], str(int(timezone.now().timestamp()))

        # Find registrations for this device
        registrations = WalletPassRegistration.objects.filter(
            device__device_library_id=device_library_id,
        ).select_related("ticket")

        # Filter by update time if provided
        if passes_updated_since:
            try:
                import datetime as dt

                since_timestamp = int(passes_updated_since)
                since_datetime = dt.datetime.fromtimestamp(since_timestamp, tz=dt.timezone.utc)
                registrations = registrations.filter(ticket__updated_at__gt=since_datetime)
            except (ValueError, TypeError):
                pass  # Invalid timestamp, return all

        serial_numbers = [str(reg.ticket_id) for reg in registrations]

        # Get the most recent update time
        if registrations.exists():
            latest = registrations.order_by("-ticket__updated_at").first()
            if latest:
                last_updated = str(int(latest.ticket.updated_at.timestamp()))
            else:
                last_updated = str(int(timezone.now().timestamp()))
        else:
            last_updated = str(int(timezone.now().timestamp()))

        return serial_numbers, last_updated

    def send_update_notifications_for_event(self, event_id: UUID) -> int:
        """Send update notifications to all devices with passes for an event.

        Called when event details change (time, location, etc.).

        Args:
            event_id: The event that was updated.

        Returns:
            Number of notifications sent successfully.
        """
        if not self.is_apple_wallet_configured():
            logger.warning("apple_wallet_not_configured_skipping_notifications")
            return 0

        # Find all registrations for tickets to this event
        registrations = WalletPassRegistration.objects.filter(
            ticket__event_id=event_id,
            device__platform=WalletPassDevice.Platform.APPLE,
        ).select_related("device", "ticket")

        if not registrations.exists():
            logger.debug("no_registrations_for_event", event_id=str(event_id))
            return 0

        # Get unique push tokens
        push_tokens = {reg.device.push_token for reg in registrations}

        success_count = 0
        for token in push_tokens:
            try:
                self.apple_push_client.send_update_notification(token)
                success_count += 1

                # Log success for related tickets
                for reg in registrations.filter(device__push_token=token):
                    WalletPassUpdateLog.objects.create(
                        ticket=reg.ticket,
                        device=reg.device,
                        update_type=WalletPassUpdateLog.UpdateType.PUSH_SENT,
                    )

            except ApplePushError as e:
                # Log failure
                for reg in registrations.filter(device__push_token=token):
                    WalletPassUpdateLog.objects.create(
                        ticket=reg.ticket,
                        device=reg.device,
                        update_type=WalletPassUpdateLog.UpdateType.PUSH_FAILED,
                        details={"error": str(e), "reason": e.reason},
                    )

        logger.info(
            "update_notifications_sent",
            event_id=str(event_id),
            total_tokens=len(push_tokens),
            successful=success_count,
        )

        return success_count

    def send_update_notification_for_ticket(self, ticket_id: UUID) -> int:
        """Send update notifications for a specific ticket.

        Args:
            ticket_id: The ticket that was updated.

        Returns:
            Number of notifications sent successfully.
        """
        if not self.is_apple_wallet_configured():
            return 0

        registrations = WalletPassRegistration.objects.filter(
            ticket_id=ticket_id,
            device__platform=WalletPassDevice.Platform.APPLE,
        ).select_related("device")

        success_count = 0
        for reg in registrations:
            try:
                self.apple_push_client.send_update_notification(reg.device.push_token)
                success_count += 1

                WalletPassUpdateLog.objects.create(
                    ticket_id=ticket_id,
                    device=reg.device,
                    update_type=WalletPassUpdateLog.UpdateType.PUSH_SENT,
                )

            except ApplePushError as e:
                WalletPassUpdateLog.objects.create(
                    ticket_id=ticket_id,
                    device=reg.device,
                    update_type=WalletPassUpdateLog.UpdateType.PUSH_FAILED,
                    details={"error": str(e)},
                )

        return success_count


# Module-level singleton instance
_wallet_service: WalletService | None = None


def get_wallet_service() -> WalletService:
    """Get the wallet service singleton.

    Returns:
        The WalletService instance.
    """
    global _wallet_service
    if _wallet_service is None:
        _wallet_service = WalletService()
    return _wallet_service
