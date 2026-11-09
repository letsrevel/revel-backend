"""Admin for the membership register."""

import typing as t

from django.contrib import admin
from django.db import transaction
from django.http import HttpRequest
from unfold.admin import ModelAdmin

from events import models
from events.admin.base import OrganizationLinkMixin
from events.service.user_note_transfer import transfer_record_note


@admin.register(models.MembershipRecord)
class MembershipRecordAdmin(ModelAdmin, OrganizationLinkMixin):  # type: ignore[misc]
    """Legal register of membership periods."""

    list_display = [
        "full_name",
        "organization_link",
        "kind",
        "tier_name",
        "valid_from",
        "valid_until",
        "ended_reason",
        "source",
    ]
    list_select_related = ["organization"]
    list_filter = ["kind", "source", "ended_reason"]
    search_fields = ["full_name", "email", "organization__name", "organization__slug"]
    autocomplete_fields = ["organization", "user"]
    raw_id_fields = [
        "tier",
        "ticket",
        "event",
        "subscription",
        "questionnaire_submission",
        "created_by",
        "duplicate_claimant",
    ]
    exclude = ["invite_token"]  # a bearer secret: never shown or editable
    readonly_fields = ["created_at", "updated_at"]

    def save_model(self, request: HttpRequest, obj: models.MembershipRecord, form: t.Any, change: bool) -> None:
        """Move the entry's private note onto the account when staff link it here."""
        was_unlinked = change and not models.MembershipRecord.objects.filter(pk=obj.pk, user__isnull=False).exists()
        with transaction.atomic():
            super().save_model(request, obj, form, change)
            if was_unlinked and obj.user_id is not None:
                transfer_record_note(obj, to_user_id=obj.user_id)
