"""Guards for the rule that a paid online 0% ticket tier needs a VAT exemption reason (#1234).

Split out of ``attendee_invoice_service`` (1000-line cap). Models only, no other service imports.
"""

import copy
import typing as t

from django.db.models import Q
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from ninja.errors import HttpError

from events.models.event import Event
from events.models.organization import Organization
from events.models.ticket import TicketTier


def zero_rate_tiers_missing_reason(org: Organization) -> bool:
    """Whether a paid online tier that can still sell is 0% rated with no exemption reason anywhere.

    Such a tier would invoice as category E without the legal basis the notice must state. One
    DB-side `.exists()`: tiers that can no longer sell (event ended or cancelled, sales window over,
    sales paused -- resuming is guarded) never flag.
    Reads `vat_rate` and `vat_exemption_reason` from `org` as given, so a not-yet-saved candidate
    state can be checked before it is persisted.

    Returns:
        `True` if at least one tier is paid, online, effectively 0% and has no reason (its own, or the
        organization's default).
    """
    if org.vat_exemption_reason.strip():
        return False  # the org default covers every tier
    pwyc = TicketTier.PriceType.PWYC
    paid = (~Q(price_type=pwyc) & Q(price__gt=0)) | (Q(price_type=pwyc) & (Q(pwyc_min__gt=0) | Q(pwyc_max__gt=0)))
    # ponytail: category_prices is {category_id: "decimal string"}; SQL can't cleanly say "any value > 0",
    # so a non-empty map counts as paid (an all-zero map over-flags; is_paid_online is exact on save).
    paid |= ~Q(price_type=pwyc) & ~Q(category_prices={}) & ~Q(category_prices__isnull=True)
    zero_rate = Q(vat_rate=0)
    if org.vat_rate == 0:
        zero_rate |= Q(vat_rate__isnull=True)  # inheriting tiers take the org default
    now = timezone.now()
    can_still_sell = Q(sales_end_at__isnull=True) | Q(sales_end_at__gte=now)
    return (
        TicketTier.objects.filter(
            paid,
            zero_rate,
            can_still_sell,
            sales_paused=False,
            event__organization=org,
            event__end__gte=now,
            payment_method=TicketTier.PaymentMethod.ONLINE,
            vat_exemption_reason__regex=r"^\s*$",
        )
        .exclude(event__status=Event.EventStatus.CANCELLED)
        .exists()
    )


def assert_billing_update_keeps_zero_rate_reasons(org: Organization, update_data: dict[str, t.Any]) -> None:
    """Reject a billing-info edit that would leave a paid online 0% tier without an exemption reason.

    Only matters while the org invoices attendees. Checks the post-update `vat_rate` and
    `vat_exemption_reason` on an unsaved copy, so nothing is written when it raises.

    Raises:
        HttpError 422: If invoicing is on and the edit would flag such a tier.
    """
    touched = {"vat_rate", "vat_exemption_reason"} & update_data.keys()
    if org.invoicing_mode == Organization.InvoicingMode.NONE or not touched:
        return
    candidate = copy.copy(org)
    for field in ("vat_rate", "vat_exemption_reason"):
        if update_data.get(field) is not None:
            setattr(candidate, field, update_data[field])
    raise_if_zero_rate_tiers_missing_reason(candidate)


def raise_if_zero_rate_tiers_missing_reason(org: Organization) -> None:
    """Raise a 422 if `org` has a paid online 0% tier without any exemption reason (see above)."""
    if zero_rate_tiers_missing_reason(org):
        raise HttpError(
            422,
            str(
                _(
                    "Some paid ticket tiers have a 0% VAT rate and no exemption reason. "
                    "Add a reason to those tiers or set a default reason for the organization."
                )
            ),
        )
