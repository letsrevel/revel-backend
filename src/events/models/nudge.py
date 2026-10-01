"""Durable log of the setup nudges Revel sent to stalled organizations."""

from django.contrib.gis.db import models

from common.models import TimeStampedModel


class OrganizationNudge(TimeStampedModel):
    """One nudge email sent to an organization's owner.

    This table — not ``Notification`` (pruned after NOTIFICATION_RETENTION_DAYS) — is what
    enforces the "never more than N" promise: the unique constraint on
    ``(organization, trigger, episode_key, sequence)`` caps each trigger at the database level.
    ``created_at`` is the send time.
    """

    class Trigger(models.TextChoices):
        """Why the org was nudged, in priority order (see org_nudge_service.RULES)."""

        DRAFT_EVENT = "draft_event", "Draft event forgotten"
        PRIVATE_PROFILE = "private_profile", "Profile still private"
        NO_EVENTS = "no_events", "No events yet"
        CHECK_IN = "check_in", "Founder check-in"
        DORMANT = "dormant", "Dormant organization"

    organization = models.ForeignKey("events.Organization", on_delete=models.CASCADE, related_name="nudges")
    trigger = models.CharField(max_length=32, choices=Trigger.choices)
    # Scopes the cap to one "episode" of a state. Empty for every trigger except DORMANT,
    # which keys on the last event's id so a revived-then-quiet org can be nudged again.
    episode_key = models.CharField(max_length=64, blank=True, default="")
    sequence = models.PositiveSmallIntegerField()
    target_event = models.ForeignKey("events.Event", on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    notification = models.ForeignKey(
        "notifications.Notification", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "trigger", "episode_key", "sequence"],
                name="unique_org_nudge_sequence",
            ),
            models.CheckConstraint(
                condition=models.Q(sequence__gte=1, sequence__lte=2),
                name="org_nudge_sequence_1_or_2",
            ),
        ]
        indexes = [models.Index(fields=["organization", "created_at"])]
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.organization_id} {self.trigger} #{self.sequence}"
