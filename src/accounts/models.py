import re
import secrets
import typing as t
import uuid
from datetime import datetime, timedelta

import pyotp
from django.conf import settings
from django.contrib.auth.models import AbstractUser, UserManager
from django.db import models
from django.db.models.functions import Lower
from django.utils import timezone
from encrypted_fields.fields import EncryptedTextField

from accounts.validators import normalize_phone_number, validate_phone_number
from common.models import TimeStampedModel


class RevelUserQueryset(models.QuerySet["RevelUser"]):
    """Queryset for RevelUser."""


class RevelUserManager(UserManager["RevelUser"]):
    def get_queryset(self) -> RevelUserQueryset:
        """Get queryset for RevelUser."""
        return RevelUserQueryset(self.model)


class RevelUser(AbstractUser):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    phone_number = models.CharField(
        max_length=20, unique=True, null=True, blank=True, validators=[validate_phone_number], help_text="Phone number"
    )
    preferred_name = models.CharField(db_index=True, max_length=255, blank=True, help_text="Preferred name")
    pronouns = models.CharField(max_length=10, blank=True, help_text="Pronouns")
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

    objects = RevelUserManager()  # type: ignore[misc]

    class Meta:
        ordering = ["username"]

    def save(self, *args: t.Any, **kwargs: t.Any) -> None:
        """Override save method to call clean()."""
        if self.phone_number:
            self.phone_number = normalize_phone_number(self.phone_number)
        super().save(*args, **kwargs)

    @property
    def display_name(self) -> str:
        """Display name."""
        return self.get_display_name()

    def get_display_name(self) -> str:
        """Returns the user's preferred name, or their full name as a fallback."""
        return (
            self.preferred_name or self.get_full_name() or re.sub(r"(\W|_)+", " ", self.username.split("@")[0]).title()
        )


class UserDataExport(TimeStampedModel):
    """Stores the user data export."""

    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending"
        PROCESSING = "PROCESSING", "Processing"
        READY = "READY", "Ready"
        FAILED = "FAILED", "Failed"

    user = models.OneToOneField(RevelUser, on_delete=models.CASCADE, related_name="data_export")
    file = models.FileField(upload_to="user_data_exports/", null=True, blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    error_message = models.TextField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    def __str__(self) -> str:
        return f"Data export for {self.user.username}"


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
