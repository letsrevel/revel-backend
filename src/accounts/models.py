import secrets
import typing as t
import uuid
from datetime import datetime, timedelta

import pyotp
from django.conf import settings
from django.contrib.auth.models import AbstractUser, UserManager
from django.core.validators import FileExtensionValidator, MaxLengthValidator
from django.db import models
from django.db.models.functions import Lower
from django.utils import timezone
from encrypted_fields.fields import EncryptedTextField

from accounts.validators import normalize_phone_number, validate_phone_number
from common.fields import (
    ALLOWED_IMAGE_EXTENSIONS,
    MarkdownField,
    ProtectedFileField,
    ProtectedImageField,
    validate_image_file,
)
from common.models import ExifStripMixin, TimeStampedModel


def profile_picture_upload_path(instance: "RevelUser", filename: str) -> str:
    """Generate upload path for profile pictures."""
    return f"profile-pictures/{instance.id}/{filename}"


class RevelUserQueryset(models.QuerySet["RevelUser"]):
    """Queryset for RevelUser."""


class RevelUserManager(UserManager["RevelUser"]):
    def get_queryset(self) -> RevelUserQueryset:
        """Get queryset for RevelUser."""
        return RevelUserQueryset(self.model)


class RevelUser(ExifStripMixin, AbstractUser):
    IMAGE_FIELDS = ("profile_picture",)

    image_validators = [
        FileExtensionValidator(allowed_extensions=ALLOWED_IMAGE_EXTENSIONS),
        validate_image_file,
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    phone_number = models.CharField(
        max_length=20, unique=True, null=True, blank=True, validators=[validate_phone_number], help_text="Phone number"
    )
    preferred_name = models.CharField(db_index=True, max_length=255, blank=True, help_text="Preferred name")
    pronouns = models.CharField(max_length=100, blank=True, help_text="Pronouns")
    email_verified = models.BooleanField(default=False)
    guest = models.BooleanField(default=False, help_text="True if this is a guest user (not fully registered)")
    totp_secret = EncryptedTextField(default=pyotp.random_base32, editable=False)
    totp_active = models.BooleanField(default=False)
    language = models.CharField(
        max_length=7,
        choices=settings.LANGUAGES,
        default=settings.LANGUAGE_CODE,
        db_index=True,
        help_text="User's preferred language",
    )
    bio = MarkdownField(
        blank=True,
        default="",
        validators=[MaxLengthValidator(500)],
        help_text="User bio (publicly visible, supports markdown)",
    )
    profile_picture = ProtectedImageField(
        upload_to=profile_picture_upload_path,
        null=True,
        blank=True,
        validators=image_validators,
    )
    profile_picture_thumbnail = ProtectedImageField(
        max_length=255,
        blank=True,
        null=True,
        help_text="150x150 thumbnail (auto-generated).",
    )
    profile_picture_preview = ProtectedImageField(
        max_length=255,
        blank=True,
        null=True,
        help_text="400x400 preview (auto-generated).",
    )

    objects = RevelUserManager()  # type: ignore[misc]

    class Meta:
        ordering = ["username"]
        indexes = [
            models.Index(
                fields=["email_verified", "is_active", "guest", "date_joined"],
                name="user_unverified_lookup_idx",
            ),
        ]

    def save(self, *args: t.Any, **kwargs: t.Any) -> None:
        """Override save method to call clean()."""
        if self.phone_number:
            self.phone_number = normalize_phone_number(self.phone_number)
        super().save(*args, **kwargs)

    def delete(self, *args: t.Any, **kwargs: t.Any) -> tuple[int, dict[str, int]]:
        """Delete profile picture thumbnails from storage when user is deleted."""
        if self.profile_picture_thumbnail:
            self.profile_picture_thumbnail.delete(save=False)
        if self.profile_picture_preview:
            self.profile_picture_preview.delete(save=False)
        return super().delete(*args, **kwargs)

    @property
    def display_name(self) -> str:
        """Display name."""
        return self.get_display_name()

    def get_display_name(self) -> str:
        """Returns the user's preferred name, or their full name as a fallback."""
        return self.preferred_name or self.get_full_name() or self.username


class UserDataExport(TimeStampedModel):
    """Stores the user data export."""

    class UserDataExportStatus(models.TextChoices):
        PENDING = "PENDING", "Pending"
        PROCESSING = "PROCESSING", "Processing"
        READY = "READY", "Ready"
        FAILED = "FAILED", "Failed"

    user = models.OneToOneField(RevelUser, on_delete=models.CASCADE, related_name="data_export")
    file = ProtectedFileField(upload_to="user_data_exports/", null=True, blank=True)
    status = models.CharField(max_length=20, choices=UserDataExportStatus.choices, default=UserDataExportStatus.PENDING)
    error_message = models.TextField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    def __str__(self) -> str:
        return f"Data export for {self.user.username}"


class EmailVerificationReminderTracking(models.Model):
    """Tracks email verification reminder state for unverified users.

    This model is created on-demand when the first reminder is sent and deleted
    when the user successfully verifies their email. It prevents spam by tracking
    when reminders were sent and ensures the exponential backoff schedule is followed.
    """

    user = models.OneToOneField(
        RevelUser, on_delete=models.CASCADE, related_name="verification_reminder_tracking", primary_key=True
    )
    last_reminder_sent_at = models.DateTimeField(
        null=True,
        blank=True,
        db_index=True,
        help_text="Timestamp when the last verification reminder email was sent",
    )
    final_warning_sent_at = models.DateTimeField(
        null=True,
        blank=True,
        db_index=True,
        help_text="Timestamp when the final 30-day warning was sent (sent only once)",
    )
    deactivation_email_sent_at = models.DateTimeField(
        null=True,
        blank=True,
        db_index=True,
        help_text="Timestamp when the account deactivation email was sent (sent only once)",
    )

    class Meta:
        verbose_name = "Email Verification Reminder Tracking"
        verbose_name_plural = "Email Verification Reminder Tracking"

    def __str__(self) -> str:
        return f"Verification tracking for {self.user.username}"


class FoodItem(TimeStampedModel):
    """Stores reusable food/ingredient names that users can create and search.

    Users can create food items for their dietary restrictions but cannot edit or delete them
    to prevent breaking references from other users' restrictions.
    """

    name = models.CharField(max_length=255, db_index=True, help_text="Food or ingredient name")

    class Meta:
        constraints = [
            models.UniqueConstraint(
                Lower("name"),
                name="unique_food_item_name_case_insensitive",
            )
        ]
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name


class DietaryRestriction(TimeStampedModel):
    """Links users to specific food items with severity levels and visibility control.

    Stores user-specific dietary restrictions (allergies, intolerances, dislikes) with optional
    notes and visibility settings for sharing with event organizers and attendees.
    """

    class RestrictionType(models.TextChoices):
        DISLIKE = "dislike", "Dislike"
        INTOLERANT = "intolerant", "Intolerant"
        ALLERGY = "allergy", "Allergy"
        SEVERE_ALLERGY = "severe_allergy", "Severe Allergy"

    user = models.ForeignKey(RevelUser, on_delete=models.CASCADE, related_name="dietary_restrictions")
    food_item = models.ForeignKey(FoodItem, on_delete=models.CASCADE, related_name="user_restrictions")
    restriction_type = models.CharField(
        max_length=20,
        choices=RestrictionType.choices,
        db_index=True,
        help_text="Severity level of the restriction",
    )
    notes = models.TextField(blank=True, help_text="Optional additional context (e.g., 'carry EpiPen')")
    is_public = models.BooleanField(
        default=False,
        help_text="If True, visible to all event attendees (aggregated); if False, only to organizers",
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["user", "food_item"],
                name="unique_user_food_item_restriction",
            )
        ]
        indexes = [
            models.Index(fields=["user", "is_public"]),
        ]
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.user.username} - {self.food_item.name} ({self.restriction_type})"


class DietaryPreference(TimeStampedModel):
    """Stores predefined lifestyle dietary choices (system-managed).

    Users can only select from existing preferences; they cannot create, edit, or delete them.
    Preferences are seeded via data migration.
    """

    name = models.CharField(
        max_length=100,
        unique=True,
        db_index=True,
        help_text="Dietary preference name (e.g., 'Vegetarian', 'Vegan', 'Gluten-Free')",
    )

    class Meta:
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name


class UserDietaryPreference(TimeStampedModel):
    """Links users to dietary preferences with optional comments and visibility control.

    Through table for M2M relationship between users and dietary preferences, allowing users
    to add context and control visibility to event organizers and attendees.
    """

    user = models.ForeignKey(RevelUser, on_delete=models.CASCADE, related_name="dietary_preferences")
    preference = models.ForeignKey(DietaryPreference, on_delete=models.CASCADE, related_name="users")
    comment = models.TextField(
        blank=True,
        help_text="Optional context (e.g., 'strictly vegan, no honey')",
    )
    is_public = models.BooleanField(
        default=False,
        help_text="If True, visible to all event attendees (aggregated); if False, only to organizers",
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["user", "preference"],
                name="unique_user_dietary_preference",
            )
        ]
        indexes = [
            models.Index(fields=["user", "is_public"]),
        ]
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.user.username} - {self.preference.name}"


# Kept for retro-compatibility


def generate_otp() -> str:
    """Generates a random 6-digit OTP."""
    return f"{secrets.randbelow(10**6):06d}"


def get_otp_expiration_time() -> datetime:
    """Returns the expiration time for OTPs."""
    return timezone.now() + timedelta(minutes=settings.ACCOUNT_OTP_EXPIRATION_MINUTES)


def get_12h_otp_expiration_time() -> datetime:
    """Returns the expiration time for story OTPs (12 hours)."""
    return timezone.now() + timedelta(hours=12)


class ImpersonationLog(models.Model):
    """Audit trail for admin impersonation events.

    Records every impersonation attempt by superusers for security auditing
    and compliance purposes. Tracks both the request creation and redemption.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    admin_user = models.ForeignKey(
        "RevelUser",
        on_delete=models.CASCADE,
        related_name="impersonations_performed",
        help_text="The superuser who initiated the impersonation",
    )
    target_user = models.ForeignKey(
        "RevelUser",
        on_delete=models.CASCADE,
        related_name="impersonations_received",
        help_text="The user being impersonated",
    )
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    ip_address = models.GenericIPAddressField(
        null=True,
        blank=True,
        help_text="IP address from which the impersonation was initiated",
    )
    user_agent = models.TextField(
        blank=True,
        default="",
        help_text="Browser/client user agent string",
    )
    token_jti = models.CharField(
        max_length=64,
        unique=True,
        db_index=True,
        help_text="JWT ID of the impersonation request token",
    )
    redeemed_at = models.DateTimeField(
        null=True,
        blank=True,
        db_index=True,
        help_text="Timestamp when the request token was exchanged for an access token",
    )

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Impersonation Log"
        verbose_name_plural = "Impersonation Logs"
        indexes = [
            models.Index(fields=["admin_user", "created_at"]),
            models.Index(fields=["target_user", "created_at"]),
        ]

    def __str__(self) -> str:
        status = "redeemed" if self.redeemed_at else "pending"
        return f"{self.admin_user} -> {self.target_user} ({status})"

    @property
    def is_redeemed(self) -> bool:
        """Check if the impersonation token has been redeemed."""
        return self.redeemed_at is not None
