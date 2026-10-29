"""Read-only admin for Stripe disputes."""

from django.contrib import admin
from django.http import HttpRequest
from unfold.admin import ModelAdmin

from events import models


@admin.register(models.Dispute)
class DisputeAdmin(ModelAdmin):  # type: ignore[misc]
    """Disputes mirrored from Stripe webhooks — resolved in the Stripe Dashboard, never edited here."""

    list_display = ["stripe_dispute_id", "organization", "status", "amount", "currency", "reason", "evidence_due_by"]
    list_filter = ["status", "reason", "currency"]
    search_fields = ["stripe_dispute_id", "stripe_charge_id", "stripe_payment_intent_id", "organization__name"]
    list_select_related = ["organization"]
    readonly_fields = [
        "id",
        "organization",
        "stripe_dispute_id",
        "stripe_charge_id",
        "stripe_payment_intent_id",
        "amount",
        "currency",
        "reason",
        "status",
        "evidence_due_by",
        "closed_at",
        "linked_payment_ids",
        "created_at",
        "updated_at",
    ]
    date_hierarchy = "created_at"

    @admin.display(description="Linked payments")
    def linked_payment_ids(self, obj: models.Dispute) -> str:
        """List the ticket Payment ids sharing this dispute's payment intent."""
        return ", ".join(str(pk) for pk in obj.linked_payments().values_list("pk", flat=True)) or "-"

    def has_add_permission(self, request: HttpRequest) -> bool:
        """Rows are recorded by the webhook handler; humans don't add them."""
        return False

    def has_change_permission(self, request: HttpRequest, obj: models.Dispute | None = None) -> bool:
        """Disputes are a mirror of Stripe state; rows are immutable here."""
        return False

    def has_delete_permission(self, request: HttpRequest, obj: models.Dispute | None = None) -> bool:
        """Disputes are a financial record; deleting is not offered."""
        return False
