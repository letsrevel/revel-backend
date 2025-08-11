from django.conf import settings
from django.contrib.gis.db import models

from accounts.models import RevelUser
from common.models import TimeStampedModel
from geo.models import City

from .event import Event
from .event_series import EventSeries
from .organization import Organization


class BaseUserPreferences(TimeStampedModel):
    class VisibilityPreference(models.TextChoices):
        ALWAYS = "always", "Always display"
        NEVER = "never", "Never display"
        TO_MEMBERS = "to_members", "Visible to other organization members"
        TO_INVITEES = "to_invitees", "Visible to other invitees at the same event"
        TO_BOTH = "to_both", "Visible to both"

    class NotificationChannelPreference(models.TextChoices):
        EMAIL = "email", "Email"
        TELEGRAM = "telegram", "Telegram"
        BOTH = "both", "Both"

    silence_all_notifications = models.BooleanField(default=False)
    show_me_on_attendee_list = models.CharField(
        choices=VisibilityPreference.choices, max_length=20, db_index=True, default=VisibilityPreference.NEVER
    )
    event_reminders = models.BooleanField(
        default=True, help_text="Receive event reminders (14, 7 and 1 days before events the user is attending)."
    )

    class Meta:
        abstract = True


class GeneralUserPreferences(BaseUserPreferences):
    user = models.OneToOneField(RevelUser, on_delete=models.CASCADE, related_name="general_preferences")
    city = models.ForeignKey(City, on_delete=models.CASCADE, null=True, blank=True)

    def __str__(self) -> str:
        return f"{self.user.username} preferences"

    class Meta:
        verbose_name_plural = "Default User Preferences"


class BaseSubscriptionPreferences(BaseUserPreferences):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="%(class)s_preferences")
    is_subscribed = models.BooleanField(default=False)

    class Meta:
        abstract = True


class UserOrganizationPreferences(BaseSubscriptionPreferences):
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="user_preferences")
    notify_on_new_events = models.BooleanField(default=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["user", "organization"],
                name="unique_organization_user_preferences",
            )
        ]


class UserEventSeriesPreferences(BaseSubscriptionPreferences):
    event_series = models.ForeignKey(EventSeries, on_delete=models.CASCADE, related_name="user_preferences")
    notify_on_new_events = models.BooleanField(default=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["user", "event_series"],
                name="unique_event_series_user_preferences",
            )
        ]


class UserEventPreferences(BaseSubscriptionPreferences):
    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name="user_preferences")
    notify_on_potluck_updates = models.BooleanField(default=False)

    def __str__(self) -> str:
        return f"{self.user.username} <> {self.event.name} preferences"

    class Meta:
        verbose_name_plural = "Event User Preferences"
        constraints = [
            models.UniqueConstraint(
                fields=["user", "event"],
                name="unique_event_user_preferences",
            )
        ]
