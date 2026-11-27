# src/events/admin/ticket.py
"""Admin classes for Ticket, TicketTier, and Payment models."""

import typing as t

from django.contrib import admin
from django.contrib.admin.utils import unquote
from django.db.models import QuerySet
from django.http import HttpRequest
from django.urls import reverse
from django.utils import timezone
from django.utils.html import format_html
from django.utils.safestring import mark_safe
from unfold.admin import ModelAdmin
from unfold.contrib.filters.admin import AutocompleteSelectFilter

from events import models
from events.admin.base import (
    EventLinkMixin,
    GrantsTemporaryTierMixin,
    UserLinkMixin,
    VenueLinkMixin,
)
from events.service import pending_checkout, ticket_service


@admin.register(models.TicketTier)
class TicketTierAdmin(GrantsTemporaryTierMixin, ModelAdmin, EventLinkMixin, VenueLinkMixin):  # type: ignore[misc]
    """Admin view for TicketTier."""

    list_display = [
        "__str__",
        "name",
        "event_link",
        "price_display",
        "price_type",
        "visibility",
        "payment_method",
        "quantity_display",
        "venue_link",
        "sector_name",
    ]
    list_select_related = ["event", "venue", "sector"]
    list_filter = [
        "visibility",
        "payment_method",
        "price_type",
        ("event", AutocompleteSelectFilter),
        ("venue", AutocompleteSelectFilter),
        ("sector", AutocompleteSelectFilter),
    ]
    list_filter_submit = True
    search_fields = ["name", "event__name", "description"]
    autocomplete_fields = ["event", "venue", "sector", "grants_membership_tier"]
    filter_horizontal = ["restricted_to_membership_tiers", "excluded_membership_tiers"]

    def delete_model(self, request: HttpRequest, obj: models.TicketTier) -> None:
        ticket_service.delete_ticket_tier(obj)

    def delete_queryset(self, request: HttpRequest, queryset: QuerySet[models.TicketTier]) -> None:
        # One checkout can span tiers, and a series pass locks the tiers of several events in pk order, but each
        # delete_ticket_tier locks only its own rows. Lock the payments and tiers of the whole selection in pk
        # order first, so the loop cannot deadlock with cancel_pending_checkout or a series pass.
        pending_checkout.lock_for_bulk_delete(
            models.Ticket.objects.filter(tier__in=queryset), tiers=models.TicketTier.objects.filter(pk__in=queryset)
        )
        for tier in queryset:
            self.delete_model(request, tier)

    @admin.display(description="Price", ordering="price")
    def price_display(self, obj: models.TicketTier) -> str:
        return f"{obj.price} {obj.currency}"

    @admin.display(description="Sold", ordering="quantity_sold")
    def quantity_display(self, obj: models.TicketTier) -> str:
        total = obj.total_quantity if obj.total_quantity is not None else "∞"
        return f"{obj.quantity_sold} / {total}"

    @admin.display(description="Sector")
    def sector_name(self, obj: models.TicketTier) -> str:
        return obj.sector.name if obj.sector else "—"


@admin.register(models.Ticket)
class TicketAdmin(ModelAdmin, UserLinkMixin, EventLinkMixin, VenueLinkMixin):  # type: ignore[misc]
    list_display = [
        "id",
        "event_link",
        "user_link",
        "tier_name",
        "venue_link",
        "sector_name",
        "seat_label",
        "status",
        "price_paid",
        "checked_in_at",
        "created_at",
    ]
    list_select_related = ["event", "user", "tier", "venue", "sector", "seat"]
    list_per_page = 50
    show_full_result_count = False
    list_filter = [
        "status",
        ("event", AutocompleteSelectFilter),
        ("tier", AutocompleteSelectFilter),
        ("venue", AutocompleteSelectFilter),
        ("sector", AutocompleteSelectFilter),
    ]
    list_filter_submit = True
    search_fields = ["event__name", "user__username", "user__email", "guest_name", "holder_email", "seat__label"]
    autocomplete_fields = ["event", "user", "tier", "checked_in_by", "venue", "sector", "seat"]
    readonly_fields = [
        "id",
        "holder_email",  # written only by the account erasure (#1476)
        "checked_in_at",
        "checked_in_by",
        "price_paid",
        "discount_code",
        "discount_amount",
        "offline_refund_amount",
        "sale_source",
        "unconfirmed_guest",
        "held_pass",
        "refund_policy_snapshot",
        "attribution",  # written once by checkout (#922); the export relies on it being an object
        "cancelled_at",
        "cancelled_by",
        "cancellation_source",
        "file_content_hash",
        "created_at",
        "updated_at",
    ]
    date_hierarchy = "created_at"

    fieldsets = [
        (None, {"fields": ["id", "event", "user", "guest_name", "holder_email", "status", "tier"]}),
        ("Seating", {"fields": ["venue", "sector", "seat"]}),
        (
            "Payment & Discounts",
            {
                "fields": [
                    "price_paid",
                    "discount_code",
                    "discount_amount",
                    "offline_refund_amount",
                    "sale_source",
                    "unconfirmed_guest",
                    "refund_policy_snapshot",
                    "held_pass",
                ]
            },
        ),
        ("Check-in", {"fields": ["checked_in_at", "checked_in_by"]}),
        (
            "Cancellation",
            {"fields": ["cancelled_at", "cancelled_by", "cancellation_source", "cancellation_reason"]},
        ),
        ("Metadata", {"fields": ["file_content_hash", "created_at", "updated_at"], "classes": ["collapse"]}),
    ]

    @admin.display(description="Tier")
    def tier_name(self, obj: models.Ticket) -> str | None:
        return obj.tier.name if obj.tier else "—"

    @admin.display(description="Sector")
    def sector_name(self, obj: models.Ticket) -> str | None:
        return obj.sector.name if obj.sector else "—"

    @admin.display(description="Seat")
    def seat_label(self, obj: models.Ticket) -> str | None:
        return obj.seat.label if obj.seat else "—"

    def delete_model(self, request: HttpRequest, obj: models.Ticket) -> None:
        ticket_service.delete_tickets(models.Ticket.objects.filter(pk=obj.pk))

    def delete_queryset(self, request: HttpRequest, queryset: QuerySet[models.Ticket]) -> None:
        ticket_service.delete_tickets(queryset)


@admin.register(models.Payment)
class PaymentAdmin(ModelAdmin, UserLinkMixin, EventLinkMixin):  # type: ignore[misc]
    """Admin for Payment model with financial tracking."""

    list_display = [
        "id_short",
        "user_link",
        "ticket_link",
        "amount_display",
        "vat_display",
        "platform_fee_display",
        "status_display",
        "stripe_session_id_short",
        "created_at",
    ]
    list_select_related = ["user", "ticket"]
    list_per_page = 50
    show_full_result_count = False
    # incident_hold_at is deliberately editable (not in readonly_fields): clearing it is
    # the operator resolution path for a stripe_session_total_mismatch hold (#756).
    list_filter = [
        "status",
        "refund_status",
        "currency",
        "created_at",
        "expires_at",
        ("incident_hold_at", admin.EmptyFieldListFilter),
    ]
    search_fields = ["user__username", "user__email", "ticket__event__name", "stripe_session_id"]
    readonly_fields = [
        "id",
        "user",
        "ticket",
        "amount",
        "currency",
        "platform_fee",
        "stripe_session_id",
        "stripe_payment_intent_id",
        "reservation_id",
        "raw_response",
        "net_amount",
        "vat_amount",
        "vat_rate",
        "platform_fee_net",
        "platform_fee_vat",
        "platform_fee_vat_rate",
        "platform_fee_reverse_charge",
        "buyer_billing_snapshot",
        "refund_amount",
        "refund_status",
        "stripe_refund_id",
        "refund_failure_reason",
        "refunded_at",
        "expires_at",
        "created_at",
        "updated_at",
    ]
    date_hierarchy = "created_at"
    ordering = ["-created_at"]

    @staticmethod
    def _unfinished_reason(payment: models.Payment) -> str | None:
        """Why the payment is an unfinished checkout (None when it is settled).

        FAILED with a session that has not expired is one too: the event-cancel sweep leaves that session open,
        and a late payment is refunded only while the row exists (#1562).
        """
        if not pending_checkout.is_unfinished_checkout(
            payment.status, payment.expires_at, payment.stripe_session_id, timezone.now()
        ):
            return None
        if payment.status == models.Payment.PaymentStatus.PENDING:
            return "pending payment: deleting it would strand its pending ticket"
        return "failed payment with an open checkout session: a late payment would have no row to reconcile"

    # Deleting an unfinished checkout (PENDING, or FAILED with an open session) would leave its ticket with no
    # payment, so the expiry sweep could never reclaim the seat. Not enforced in has_delete_permission:
    # Django checks it for every object a cascade removes, so it would also make deleting an event, tier or
    # ticket with a pending checkout a 403.
    def get_deleted_objects(self, objs: t.Any, request: HttpRequest) -> t.Any:
        deleted, model_count, perms_needed, protected = super().get_deleted_objects(objs, request)
        protected = [
            *protected,
            *(
                # The pk, not str(payment): __str__ reads the ticket, one query per row.
                f"Payment {payment.pk} ({reason})"
                for payment in objs
                if (reason := self._unfinished_reason(payment))
            ),
        ]
        return deleted, model_count, perms_needed, protected

    def change_view(
        self, request: HttpRequest, object_id: str, form_url: str = "", extra_context: dict[str, t.Any] | None = None
    ) -> t.Any:
        obj = self.get_object(request, unquote(object_id))
        if obj is None:
            return super().change_view(request, object_id, form_url, extra_context)
        unfinished = self._unfinished_reason(obj) is not None
        return super().change_view(
            request, object_id, form_url, {**(extra_context or {}), "show_delete": not unfinished}
        )

    fieldsets = [
        (None, {"fields": ["id", "user", "ticket", "status", "stripe_session_id"]}),
        ("Amounts", {"fields": ["amount", "currency", "net_amount", "vat_amount", "vat_rate"]}),
        (
            "Platform Fee",
            {
                "fields": [
                    "platform_fee",
                    "platform_fee_net",
                    "platform_fee_vat",
                    "platform_fee_vat_rate",
                    "platform_fee_reverse_charge",
                ]
            },
        ),
        (
            "Refund",
            {"fields": ["refund_status", "refund_amount", "stripe_refund_id", "refund_failure_reason", "refunded_at"]},
        ),
        ("Billing Snapshot", {"fields": ["buyer_billing_snapshot"], "classes": ["collapse"]}),
        ("Stripe", {"fields": ["stripe_payment_intent_id", "reservation_id", "raw_response"]}),
        ("Dates", {"fields": ["expires_at", "created_at", "updated_at"]}),
    ]

    @admin.display(description="ID")
    def id_short(self, obj: models.Payment) -> str:
        return str(obj.id)[:8] + "..."

    @admin.display(description="Ticket")
    def ticket_link(self, obj: models.Payment) -> str:
        url = reverse("admin:events_ticket_change", args=[obj.ticket.id])
        return format_html('<a href="{}">{}</a>', url, f"Ticket {str(obj.ticket.id)[:8]}...")

    @admin.display(description="Amount")
    def amount_display(self, obj: models.Payment) -> str:
        return f"{obj.amount} {obj.currency}"

    @admin.display(description="Status")
    def status_display(self, obj: models.Payment) -> str:
        colors: dict[t.Any, str] = {
            models.Payment.PaymentStatus.PENDING: "orange",
            models.Payment.PaymentStatus.SUCCEEDED: "green",
            models.Payment.PaymentStatus.FAILED: "red",
            models.Payment.PaymentStatus.REFUNDED: "blue",
        }
        color = colors.get(obj.status, "gray")
        return mark_safe(f'<span style="color: {color};">{obj.get_status_display()}</span>')

    @admin.display(description="VAT")
    def vat_display(self, obj: models.Payment) -> str:
        if obj.vat_amount is not None:
            return f"{obj.vat_amount} ({obj.vat_rate}%)"
        return "—"

    @admin.display(description="P. Fee")
    def platform_fee_display(self, obj: models.Payment) -> str:
        if obj.platform_fee_reverse_charge:
            return f"{obj.platform_fee} (RC)"
        if obj.platform_fee_net is not None:
            parts = [f"{obj.platform_fee}"]
            if obj.platform_fee_vat:
                parts.append(f"VAT {obj.platform_fee_vat}")
            return " / ".join(parts)
        if obj.platform_fee:
            return str(obj.platform_fee)
        return "—"

    @admin.display(description="Stripe Session")
    def stripe_session_id_short(self, obj: models.Payment) -> str:
        if obj.stripe_session_id:
            return obj.stripe_session_id[:20] + "..."
        return "-"
