"""Country-agnostic enforcement on top of the resolved policies.

Services call these; none of them knows which country it is in. Every helper works
out which countries reach a sale, and through which :class:`~events.compliance.base.Nexus`,
then asks each of those countries' policies. Refusals raise
:class:`~events.exceptions.CountryComplianceError` (422 ``{"detail"}``) carrying the
policy's translated reason.
"""

import typing as t
from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal

from events.compliance.base import (
    ESTABLISHMENT_ONLY,
    AttendeeInvoicingCapability,
    BuyerContext,
    ComplianceNotice,
    Decision,
    Nexus,
    PaymentChannelCapability,
    TicketComplianceField,
    channel_capability,
    common_ticket_fields,
)
from events.compliance.registry import (
    get_policy,
    get_policy_for_country,
    normalize_country_code,
    resolve_org_jurisdiction,
)
from events.exceptions import CountryComplianceError
from events.models import Organization, TicketTier
from events.utils.tier_pricing import parse_price_map

if t.TYPE_CHECKING:
    from events.models import Event, Ticket

NexusMap = dict[str, frozenset[Nexus]]


def _raise_if_blocked(decision: Decision) -> None:
    """Raise ``CountryComplianceError`` with the policy's reason when the decision refuses."""
    if not decision.allowed:
        raise CountryComplianceError(decision.reason)


def sale_nexus(org: Organization, events: t.Iterable["Event"] = (), venue_countries: t.Iterable[str] = ()) -> NexusMap:
    """Which countries reach a sale, and how: the org's establishment, and each physical event's venue.

    The establishment is keyed by the org's jurisdiction, which may be a subdivision with
    its own policy (``ES-PV``); venues are always plain countries. Virtual events have no
    venue nexus. ``venue_countries`` adds venues known only as a country code (e.g. an
    invoice whose event was deleted).
    """
    reach: dict[str, set[Nexus]] = defaultdict(set)
    if country := resolve_org_jurisdiction(org):
        reach[country].add(Nexus.ESTABLISHMENT)
    venues = [event.effective_vat_country for event in events if not event.is_virtual]
    for country in (normalize_country_code(c) for c in [*venues, *venue_countries]):
        if country:
            reach[country].add(Nexus.VENUE)
    return {country: frozenset(how) for country, how in reach.items()}


def _first_refusal(decisions: t.Iterable[Decision]) -> Decision:
    """The first refusal among ``decisions`` (the strictest country wins), else an approval."""
    return next((decision for decision in decisions if not decision.allowed), Decision.allow())


# --- Attendee invoicing -------------------------------------------------------------


def assert_attendee_invoicing_allowed(org: Organization) -> None:
    """Refuse enabling attendee invoicing when the org's own country refuses a consumer invoice.

    Raises:
        CountryComplianceError: If the organization's country blocks attendee invoicing today.
    """
    _raise_if_blocked(get_policy(org).attendee_invoicing(BuyerContext(), ESTABLISHMENT_ONLY))


def attendee_invoicing_active(event: "Event") -> bool:
    """Whether the event's org has invoicing on and Revel may invoice a consumer for this event today.

    The same decision invoice generation makes: the org's establishment and the event's
    venue both reach the sale (#1107). Select ``organization__city``, ``venue__city`` and
    ``city`` to keep this query-free.
    """
    org = event.organization
    return (
        org.invoicing_mode != Organization.InvoicingMode.NONE
        and attendee_invoicing_for_sale(sale_nexus(org, [event]), BuyerContext()).allowed
    )


def attendee_invoicing_for_sale(reach: NexusMap, buyer: BuyerContext) -> Decision:
    """The strictest invoicing decision among the countries reaching one sale, for one buyer.

    Args:
        reach: The countries reaching the sale (see :func:`sale_nexus`).
        buyer: Who the invoice is for; build it with :meth:`BuyerContext.from_billing_snapshot`.
    """
    return _first_refusal(
        get_policy_for_country(country).attendee_invoicing(buyer, nexus) for country, nexus in reach.items()
    )


# --- Payment channels ---------------------------------------------------------------


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


def payment_channel_decision(org: Organization, payment_method: str, events: t.Iterable["Event"]) -> Decision:
    """May money be taken through this tier/pass payment method for these events?

    ONLINE is the online channel; OFFLINE and AT_THE_DOOR (staff-confirmed) the offline
    one; FREE takes no money.
    """
    return _channel_decision(sale_nexus(org, events), payment_method)


def _channel_decision(reach: NexusMap, payment_method: str) -> Decision:
    """Ask every reaching country's policy about the channel ``payment_method`` uses."""
    policies = [(get_policy_for_country(c), nexus) for c, nexus in reach.items()]
    if payment_method == TicketTier.PaymentMethod.ONLINE:
        return _first_refusal(policy.online_payment(nexus) for policy, nexus in policies)
    if payment_method in (TicketTier.PaymentMethod.OFFLINE, TicketTier.PaymentMethod.AT_THE_DOOR):
        return _first_refusal(policy.offline_payment(nexus) for policy, nexus in policies)
    return Decision.allow()


def organizer_notices(reach: NexusMap) -> list[ComplianceNotice]:
    """Every reaching country's non-blocking organizer notices, once each (by key)."""
    notices = [n for c, nexus in reach.items() for n in get_policy_for_country(c).organizer_notices(nexus)]
    return list({notice.key: notice for notice in notices}.values())


@dataclass(frozen=True, slots=True)
class EventCompliance:
    """What the countries reaching one event allow today: the per-event view of the gates.

    Built from the same nexus map and decisions the tier, checkout and invoicing gates
    use, so the frontend can disable exactly what the API would refuse.
    """

    venue_country: str
    online_payment: PaymentChannelCapability
    offline_payment: PaymentChannelCapability
    attendee_invoicing: AttendeeInvoicingCapability
    notices: list[ComplianceNotice]


def event_compliance(event: "Event") -> EventCompliance:
    """The effective compliance capabilities of one event (org establishment + physical venue, today).

    Select ``organization__city``, ``venue__city`` and ``city`` to keep this query-free.
    """
    reach = sale_nexus(event.organization, [event])
    if not attendee_invoicing_for_sale(reach, BuyerContext()).allowed:
        invoicing = AttendeeInvoicingCapability.BLOCKED
    elif any(not attendee_invoicing_for_sale(reach, BuyerContext(vat_country=code[:2])).allowed for code in reach):
        # A buyer whose VAT ID is from one of the reaching countries (a subdivision's: its country) would be refused.
        invoicing = AttendeeInvoicingCapability.BLOCKED_FOR_BUSINESS_BUYERS
    else:
        invoicing = AttendeeInvoicingCapability.ALLOWED
    return EventCompliance(
        venue_country="" if event.is_virtual else normalize_country_code(event.effective_vat_country),
        online_payment=channel_capability(_channel_decision(reach, TicketTier.PaymentMethod.ONLINE)),
        offline_payment=channel_capability(_channel_decision(reach, TicketTier.PaymentMethod.OFFLINE)),
        attendee_invoicing=invoicing,
        notices=organizer_notices(reach),
    )


def assert_sale_allowed(
    org: Organization, payment_method: str, unit_prices: t.Iterable[Decimal], events: t.Iterable["Event"]
) -> None:
    """Refuse a sale that costs anything through a payment channel blocked where it is reached.

    Args:
        org: The selling organization.
        payment_method: The tier's / pass's payment method.
        unit_prices: What the buyer pays per ticket/pass, as priced at checkout.
        events: The events the sale admits to.

    Raises:
        CountryComplianceError: If money would be taken through a blocked channel.
    """
    _raise_if_blocked(sale_decision(org, payment_method, unit_prices, events))


def sale_decision(
    org: Organization, payment_method: str, unit_prices: t.Iterable[Decimal], events: t.Iterable["Event"]
) -> Decision:
    """The decision :func:`assert_sale_allowed` enforces: free sales are always allowed."""
    if any(price > 0 for price in unit_prices):
        return payment_channel_decision(org, payment_method, events)
    return Decision.allow()


def series_pass_online_payment(
    org: Organization, payment_method: str, price: Decimal, events: t.Iterable["Event"]
) -> PaymentChannelCapability:
    """Whether a pass checkout at ``price`` for ``events`` takes money online where it is allowed (#1081).

    For an ONLINE pass this is the decision the pass checkout gate makes. Offline and free
    passes don't use the online channel and read ``allowed``.
    """
    if payment_method != TicketTier.PaymentMethod.ONLINE:
        return PaymentChannelCapability.ALLOWED
    return channel_capability(sale_decision(org, payment_method, [price], events))


def tier_channel_decision(tier: TicketTier) -> Decision:
    """Whether a tier, as configured, would take money through a blocked channel."""
    if not tier_is_paid(tier):
        return Decision.allow()
    return payment_channel_decision(tier.event.organization, tier.payment_method, [tier.event])


def assert_tier_allowed(tier: TicketTier, *, was_allowed: bool = True) -> None:
    """Gate a tier create or update against the payment-channel policies.

    Refuses a new tier, an update into a configuration, or resuming sales of a tier,
    when the result would take money through a blocked channel. Resuming counts as a
    create (#945: "200 on resume, then a dead checkout" is a bug). A tier already in such
    a configuration before the gate otherwise stays editable (rename, pause, switch to
    offline or free): checkout refuses selling it either way, so organizers are never
    locked out of their own data.

    Args:
        tier: The tier in its would-be state (unsaved on create, mutated on update), with ``event`` set.
        was_allowed: :func:`tier_channel_decision` of the stored tier before the update; True on
            create and when the update resumes sales.

    Raises:
        CountryComplianceError: If the tier would newly use a blocked payment channel.
    """
    if was_allowed:
        _raise_if_blocked(tier_channel_decision(tier))


# --- Ticket content -----------------------------------------------------------------


def ticket_fields(ticket: "Ticket") -> list[TicketComplianceField]:
    """Every compliance line for a ticket: the EU common set, then each reaching country's additions."""
    reach = sale_nexus(ticket.event.organization, [ticket.event])
    extras = [
        field for c, nexus in reach.items() for field in get_policy_for_country(c).extra_ticket_fields(ticket, nexus)
    ]
    return [*common_ticket_fields(ticket), *extras]
