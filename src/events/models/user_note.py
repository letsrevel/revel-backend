"""Private organizer notes about a person."""

from django.conf import settings
from django.contrib.gis.db import models
from django.core.exceptions import ValidationError
from django.db.models import Q
from django.utils.translation import gettext_lazy as _

from common.fields import MarkdownField
from common.models import TimeStampedModel

USER_NOTE_MAX_LENGTH = 5000


class OrganizationUserNote(TimeStampedModel):
    """One private, editable note an organization keeps about a user or an account-less register entry.

    A note targets exactly one of ``user`` or ``record`` (never both). It is keyed on
    ``(organization, user)`` rather than on ``OrganizationMember`` so the note survives
    ``remove_member`` and also exists for attendees who were never members. A register entry without an
    account (``record``) can carry its own note; it moves onto the user's note when the entry gets linked
    to an account. Visible to staff only; it must never be serialized by a member- or attendee-facing
    schema, an export or a notification.
    """

    organization = models.ForeignKey("events.Organization", on_delete=models.CASCADE, related_name="user_notes")
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, null=True, blank=True, related_name="organizer_notes"
    )
    record = models.ForeignKey(
        "events.MembershipRecord", on_delete=models.CASCADE, null=True, blank=True, related_name="organizer_notes"
    )
    body = MarkdownField()
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=Q(user__isnull=False, record__isnull=True) | Q(user__isnull=True, record__isnull=False),
                name="org_note_user_xor_record",
            ),
            models.UniqueConstraint(
                fields=["organization", "user"], condition=Q(user__isnull=False), name="unique_org_user_note"
            ),
            models.UniqueConstraint(
                fields=["organization", "record"], condition=Q(record__isnull=False), name="unique_org_record_note"
            ),
        ]

    def clean(self) -> None:
        """A record note must target a register entry of the same organization."""
        super().clean()
        if self.record is not None and self.record.organization_id != self.organization_id:
            raise ValidationError(str(_("The register entry belongs to another organization.")))

    def __str__(self) -> str:
        # Admin LogEntry.object_repr stores this: it must not identify the subject.
        return f"Private note {self.pk}"
