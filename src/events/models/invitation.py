import typing as t

from django.conf import settings
from django.contrib.gis.db import models
from django.db.models import Prefetch, Q
from django.utils import timezone

from accounts.models import RevelUser
from common.models import TagAssignment, TimeStampedModel

from .mixins import TokenMixin, UserRequestMixin


class EventInvitationQueryset(models.QuerySet["EventInvitation"]):
    def with_related(self) -> t.Self:
        """Prefetch related objects for invitations."""
        return self.select_related("event", "user", "tier")

    def with_event_details(self) -> t.Self:
        """Prefetch event with its nested relations for list views."""
        return self.select_related(
            "event",
            "event__organization",
            "event__event_series",
            "event__city",
            "user",
            "tier",
        ).prefetch_related(
            Prefetch(
                "event__tags",
                queryset=TagAssignment.objects.select_related("tag"),
                to_attr="prefetched_tagassignments",
            )
        )

    def for_user(self, user: RevelUser) -> t.Self:
        """Get the base invitation qs for a user."""
        today = timezone.now().date()
        return self.filter(Q(user=user) & (Q(event__start__date__gt=today) | Q(event__start__isnull=True)))


class EventInvitationManager(models.Manager["EventInvitation"]):
    def get_queryset(self) -> EventInvitationQueryset:
        """Get base queryset for invitations."""
        return EventInvitationQueryset(self.model, using=self._db)

    def for_user(self, user: RevelUser) -> EventInvitationQueryset:
        """Get the base invitation qs for a user."""
        return self.get_queryset().for_user(user)

    def with_related(self) -> EventInvitationQueryset:
        """Returns a queryset with related objects."""
        return self.get_queryset().with_related()

    def with_event_details(self) -> EventInvitationQueryset:
        """Returns a queryset with event and nested relations."""
        return self.get_queryset().with_event_details()


class AbstractEventInvitation(TimeStampedModel):
    """Abstract base class for event invitations with common fields."""

    event = models.ForeignKey("events.Event", on_delete=models.CASCADE)
    waives_questionnaire = models.BooleanField(default=False)
    waives_purchase = models.BooleanField(default=False)
    overrides_max_attendees = models.BooleanField(default=False)
    waives_membership_required = models.BooleanField(default=False)
    waives_rsvp_deadline = models.BooleanField(default=False, help_text="Waives RSVP deadline check for this user")
    waives_apply_deadline = models.BooleanField(
        default=False, help_text="Waives application deadline check for this user"
    )
    custom_message = models.TextField(null=True, blank=True)
    tier = models.ForeignKey("events.TicketTier", on_delete=models.SET_NULL, null=True, blank=True)

    class Meta:
        abstract = True


class EventInvitation(AbstractEventInvitation):
    """Event invitation for registered users."""

    event = models.ForeignKey("events.Event", on_delete=models.CASCADE, related_name="invitations")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="invitations")

    objects = EventInvitationManager()

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["event", "user"],
                name="unique_event_invitation_event_user",
            )
        ]
        ordering = ["-created_at"]


class PendingEventInvitation(AbstractEventInvitation):
    """Event invitation for unregistered users (by email)."""

    event = models.ForeignKey("events.Event", on_delete=models.CASCADE, related_name="pending_invitations")
    email = models.EmailField(db_index=True, help_text="Email of the user to be invited")

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["event", "email"],
                name="unique_pending_event_invitation_event_email",
            )
        ]
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"Pending invitation for {self.email} to {self.event.name}"


class EventToken(TokenMixin):
    event = models.ForeignKey("events.Event", on_delete=models.CASCADE, related_name="tokens")
    grants_invitation = models.BooleanField(default=True)
    ticket_tier = models.ForeignKey("events.TicketTier", on_delete=models.CASCADE, null=True, blank=True)
    invitation_payload = models.JSONField(
        null=True, blank=True, help_text="If provided, the token will we viable to claim invitations."
    )

    class Meta:
        indexes = [
            # For listing active tokens by event
            models.Index(fields=["event", "expires_at"], name="eventtoken_event_expires"),
            # For listing tokens by event ordered by creation
            models.Index(fields=["event", "-created_at"], name="eventtoken_event_created"),
        ]
        ordering = ["-created_at"]


class EventInvitationRequest(UserRequestMixin):
    class InvitationRequestStatus(models.TextChoices):
        PENDING = "pending"
        APPROVED = "approved"
        REJECTED = "rejected"

    event = models.ForeignKey("events.Event", on_delete=models.CASCADE, related_name="invitation_requests")

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["event", "user"],
                name="unique_event_user_invitation_request",
            )
        ]
        ordering = ["-created_at"]
