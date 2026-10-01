"""Country-agnostic enforcement on top of the resolved policies.

Services call these; none of them knows which country it is in. Refusals raise
:class:`~events.exceptions.CountryComplianceError` (422 ``{"detail"}``) carrying the
policy's translated reason.
"""

import typing as t
from decimal import Decimal

from events.compliance.base import BuyerContext, Decision
from events.compliance.registry import get_policy, get_policy_for_country, resolve_org_country
from events.exceptions import CountryComplianceError
from events.models import Organization, TicketTier
from events.utils.tier_pricing import parse_price_map

if t.TYPE_CHECKING:
    from events.models import Event


def _raise_if_blocked(decision: Decision) -> None:
    if not decision.allowed:
        raise CountryComplianceError(decision.reason)


def assert_attendee_invoicing_allowed(org: Organization) -> None:
    """Refuse enabling attendee invoicing when the org's policy refuses a consumer invoice.

    A country that only blocks domestic B2B invoices may still enable it; those sales
    are skipped at generation time.

    Raises:
        CountryComplianceError: If the organization's country blocks attendee invoicing.
    """
    _raise_if_blocked(get_policy(org).attendee_invoicing(BuyerContext()))


def attendee_invoicing_active(org: Organization) -> bool:
    """Whether the org has invoicing on and its policy issues invoices to consumers."""
    return (
        org.invoicing_mode != Organization.InvoicingMode.NONE
        and get_policy(org).attendee_invoicing(BuyerContext()).allowed
    )


def liable_countries(org: Organization, event: "Event | None") -> list[str]:
    """Countries whose rules can bind a sale: the org's and, for a physical event, the venue's.

    Physical admission is taxed where the event takes place (#869), so an organizer
    selling into a restricted country is gated as well as one established there.
    """
    countries = [resolve_org_country(org)]
    if event is not None and not event.is_virtual:
        countries.append(event.effective_vat_country)
    return countries


def attendee_invoicing_for_sale(countries: t.Iterable[str], buyer_vat_id: str | None) -> Decision:
    """The strictest invoicing decision among the liable countries for one buyer."""
    buyer = BuyerContext.from_vat_id(buyer_vat_id)
    for country in dict.fromkeys(c for c in countries if c):
        decision = get_policy_for_country(country).attendee_invoicing(buyer)
        if not decision.allowed:
            return decision
    return Decision.allow()


def tier_is_paid(tier: TicketTier) -> bool:
    """Whether a tier can charge the buyer anything (flat price, PWYC or any category price).

    Accepts a tier whose fields still hold JSON-dumped payload values (``"20.00"``),
    as the tier services set them before ``full_clean`` coerces them.
    """
    if tier.price_type == TicketTier.PriceType.PWYC:
        return True  # pwyc_min is validated >= 1
    if Decimal(str(tier.price or 0)) > 0:
        return True
    return any(price > 0 for price in parse_price_map(tier.category_prices).values())


def assert_paid_ticketing_allowed(org: Organization, events: t.Iterable["Event"] = ()) -> None:
    """Refuse a paid sale where any liable country's policy blocks paid ticketing.

    Args:
        org: The selling organization.
        events: The events being sold; a physical event binds its venue's country too.

    Raises:
        CountryComplianceError: If the org's country, or a physical event's, blocks paid ticketing.
    """
    countries = [resolve_org_country(org)]
    countries += [country for event in events for country in liable_countries(org, event)[1:]]
    for country in dict.fromkeys(c for c in countries if c):
        _raise_if_blocked(get_policy_for_country(country).paid_ticketing())


def assert_sale_allowed(org: Organization, unit_prices: t.Iterable[Decimal], events: t.Iterable["Event"] = ()) -> None:
    """Refuse a sale that costs anything where paid ticketing is blocked (see above).

    Args:
        org: The selling organization.
        unit_prices: What the buyer pays per ticket/pass, as priced at checkout.
        events: The events the sale admits to.

    Raises:
        CountryComplianceError: If anything costs money and paid ticketing is blocked.
    """
    if any(price > 0 for price in unit_prices):
        assert_paid_ticketing_allowed(org, events)


def assert_tier_allowed(tier: TicketTier, *, was_paid: bool = False) -> None:
    """Gate a tier create or update against the paid-ticketing policy.

    Refuses a new paid tier and an update that turns a free tier into a paid one. A
    paid tier that predates the gate stays editable (rename, pause, make free):
    checkout refuses selling it either way, so this gate is about not creating new
    paid offerings, not about locking organizers out of their own data.

    Args:
        tier: The tier in its would-be state (unsaved on create, mutated on update),
            with ``event`` set.
        was_paid: :func:`tier_is_paid` of the stored tier before the update; False on create.

    Raises:
        CountryComplianceError: If a paid tier would be created where paid ticketing is blocked.
    """
    if not was_paid and tier_is_paid(tier):
        assert_paid_ticketing_allowed(tier.event.organization, [tier.event])
