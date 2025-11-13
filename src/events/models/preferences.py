"""User preference models."""

from django.contrib.gis.db import models

from accounts.models import RevelUser
from common.models import TimeStampedModel
from geo.models import City


class BaseUserPreferences(TimeStampedModel):
    """Base class for user preferences with visibility settings."""

    class VisibilityPreference(models.TextChoices):
        ALWAYS = "always", "Always display"
        NEVER = "never", "Never display"
        TO_MEMBERS = "to_members", "Visible to other organization members"
        TO_INVITEES = "to_invitees", "Visible to other invitees at the same event"
        TO_BOTH = "to_both", "Visible to both"

    show_me_on_attendee_list = models.CharField(
        choices=VisibilityPreference.choices, max_length=20, db_index=True, default=VisibilityPreference.NEVER
    )

    class Meta:
        abstract = True


class GeneralUserPreferences(BaseUserPreferences):
    """General user preferences for visibility and location.

    For notification preferences, see notifications.models.NotificationPreference.
    """

    user = models.OneToOneField(RevelUser, on_delete=models.CASCADE, related_name="general_preferences")
    city = models.ForeignKey(City, on_delete=models.CASCADE, null=True, blank=True)

    def __str__(self) -> str:
        return f"{self.user.username} preferences"

    class Meta:
        verbose_name_plural = "Default User Preferences"
