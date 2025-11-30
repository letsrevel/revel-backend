"""Models for wallet pass device registration and tracking.

This module provides models for tracking device registrations for wallet
pass updates. When a user adds a pass to their wallet, the device registers
with our server to receive push notifications when the pass needs updating.
"""

import secrets
import typing as t

from django.db import models

from common.models import TimeStampedModel

if t.TYPE_CHECKING:
    pass


AUTH_TOKEN_LENGTH = 32


def generate_auth_token() -> str:
    """Generate a secure authentication token for pass validation."""
    return secrets.token_urlsafe(AUTH_TOKEN_LENGTH)


class WalletPassDevice(TimeStampedModel):
    """Represents a device registered for wallet pass updates.

    When a pass is added to a user's wallet (Apple Wallet, Google Wallet),
    the device registers with our server by providing a unique device
    library identifier and a push token for sending update notifications.
    """

    class Platform(models.TextChoices):
        """Supported wallet platforms."""

        APPLE = "apple", "Apple Wallet"
        GOOGLE = "google", "Google Wallet"

    device_library_id = models.CharField(
        max_length=255,
        unique=True,
        db_index=True,
        help_text="Unique identifier provided by the wallet app for this device.",
    )
    push_token = models.CharField(
        max_length=255,
        help_text="Token used to send push notifications to this device.",
    )
    platform = models.CharField(
        max_length=10,
        choices=Platform.choices,
        default=Platform.APPLE,
        db_index=True,
    )

    class Meta:
        verbose_name = "Wallet Pass Device"
        verbose_name_plural = "Wallet Pass Devices"

    def __str__(self) -> str:
        return f"{self.get_platform_display()} Device {self.device_library_id[:8]}..."


class WalletPassRegistration(TimeStampedModel):
    """Links a ticket to devices that want to receive pass updates.

    This is a many-to-many relationship: a ticket can be registered on
    multiple devices (e.g., user's phone and watch), and a device can
    have multiple passes registered.

    The authentication token is used to validate requests from Apple/Google
    when they fetch pass updates.
    """

    ticket = models.ForeignKey(
        "events.Ticket",
        on_delete=models.CASCADE,
        related_name="wallet_registrations",
    )
    device = models.ForeignKey(
        WalletPassDevice,
        on_delete=models.CASCADE,
        related_name="registrations",
    )
    auth_token = models.CharField(
        max_length=64,
        default=generate_auth_token,
        db_index=True,
        help_text="Authentication token for validating pass update requests.",
    )

    class Meta:
        verbose_name = "Wallet Pass Registration"
        verbose_name_plural = "Wallet Pass Registrations"
        constraints = [
            models.UniqueConstraint(
                fields=["ticket", "device"],
                name="unique_ticket_device_registration",
            )
        ]
        indexes = [
            models.Index(fields=["ticket", "device"]),
            models.Index(fields=["auth_token"]),
        ]

    def __str__(self) -> str:
        return f"Registration: {self.ticket} on {self.device}"

    @property
    def serial_number(self) -> str:
        """Get the pass serial number (ticket UUID as string)."""
        return str(self.ticket_id)


class WalletPassUpdateLog(TimeStampedModel):
    """Log of pass updates for debugging and auditing.

    Tracks when passes were updated and notifications sent, useful for
    debugging issues with pass updates not being received.
    """

    class UpdateType(models.TextChoices):
        """Types of pass update events."""

        PASS_GENERATED = "generated", "Pass Generated"
        PUSH_SENT = "push_sent", "Push Notification Sent"
        PUSH_FAILED = "push_failed", "Push Notification Failed"
        PASS_FETCHED = "fetched", "Pass Fetched by Device"
        DEVICE_REGISTERED = "registered", "Device Registered"
        DEVICE_UNREGISTERED = "unregistered", "Device Unregistered"

    ticket = models.ForeignKey(
        "events.Ticket",
        on_delete=models.CASCADE,
        related_name="wallet_update_logs",
        null=True,
        blank=True,
    )
    device = models.ForeignKey(
        WalletPassDevice,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="update_logs",
    )
    update_type = models.CharField(
        max_length=20,
        choices=UpdateType.choices,
        db_index=True,
    )
    details = models.JSONField(
        default=dict,
        blank=True,
        help_text="Additional details about the update event.",
    )

    class Meta:
        verbose_name = "Wallet Pass Update Log"
        verbose_name_plural = "Wallet Pass Update Logs"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["ticket", "-created_at"]),
            models.Index(fields=["update_type", "-created_at"]),
        ]

    def __str__(self) -> str:
        return f"{self.get_update_type_display()} - {self.created_at}"
