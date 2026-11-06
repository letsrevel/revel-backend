"""Private organizer notes about a person."""

from django.conf import settings
from django.contrib.gis.db import models
from django.core.validators import MaxLengthValidator

from common.fields import MarkdownField
from common.models import TimeStampedModel

USER_NOTE_MAX_LENGTH = 5000


class OrganizationUserNote(TimeStampedModel):
    """One private, editable note an organization keeps about a user.

    Keyed on ``(organization, user)`` rather than on ``OrganizationMember`` so the note survives
    ``remove_member`` and also exists for attendees who were never members. Visible to staff only;
    it must never be serialized by a member- or attendee-facing schema, an export or a notification.
    """

    organization = models.ForeignKey("events.Organization", on_delete=models.CASCADE, related_name="user_notes")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="organizer_notes")
    body = MarkdownField(validators=[MaxLengthValidator(USER_NOTE_MAX_LENGTH)])
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["organization", "user"], name="unique_org_user_note"),
        ]

    def __str__(self) -> str:
        return f"Note on {self.user_id} by {self.organization_id}"
