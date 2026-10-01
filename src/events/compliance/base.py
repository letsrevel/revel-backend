"""The country-policy contract, the EU default, and the reusable restriction mixins.

A country module subclasses :class:`DefaultEUPolicy` (usually through one of the
mixins below) and registers itself with :func:`events.compliance.registry.register`.
Call sites only ever talk to the resolved policy object.

Restrictions are deliberately minimal: each mixin blocks exactly the one feature a
country's law makes non-compliant, only for the sales that law reaches (see
:class:`Nexus`), and only from the date it takes effect.
"""

import abc
import datetime
import enum
import gettext
import typing as t
from dataclasses import dataclass
from decimal import Decimal

import pycountry
from django.utils import timezone, translation
from django.utils.translation import gettext_lazy as _

if t.TYPE_CHECKING:
    from django_stubs_ext import StrPromise

    from events.models import Ticket


class AttendeeInvoicingCapability(enum.StrEnum):
    """Whether Revel may issue attendee invoices in the organizer's name."""

    ALLOWED = "allowed"
    BLOCKED = "blocked"
    # Domestic B2B invoices must go through a national e-invoicing network.
    BLOCKED_FOR_BUSINESS_BUYERS = "blocked_for_business_buyers"


class PaymentChannelCapability(enum.StrEnum):
    """Whether a payment channel (online card checkout, or offline/at-the-door) may be used."""

    ALLOWED = "allowed"
    BLOCKED = "blocked"


class Nexus(enum.StrEnum):
    """Why a country's rules reach a sale."""

    ESTABLISHMENT = "establishment"  # the organizer is established there
    VENUE = "venue"  # the physical event takes place there


ALL_NEXUS: t.Final = frozenset(Nexus)


@dataclass(frozen=True, slots=True)
class Decision:
    """A policy verdict; ``reason`` is the translated, user-facing explanation when refused."""

    allowed: bool
    reason: str = ""

    @classmethod
    def allow(cls) -> "Decision":
        """An approval."""
        return cls(allowed=True)

    @classmethod
    def block(cls, reason: str) -> "Decision":
        """A refusal carrying its explanation."""
        return cls(allowed=False, reason=reason)


@dataclass(frozen=True, slots=True)
class BuyerContext:
    """Who an invoice would be addressed to. ``vat_country`` is empty for consumers."""

    vat_country: str = ""

    @classmethod
    def from_vat_id(cls, vat_id: str | None) -> "BuyerContext":
        """Build from the buyer's VAT ID (its two-letter prefix, ``EL`` normalized to ``GR``)."""
        from events.compliance.registry import normalize_country_code

        return cls(vat_country=normalize_country_code((vat_id or "").strip()[:2]))


@dataclass(frozen=True, slots=True)
class TicketComplianceField:
    """One extra line printed on the ticket PDF and wallet passes."""

    key: str
    label: str
    value: str


ATTENDEE_INVOICING_BLOCKED_MESSAGE = _(
    "Revel can't issue invoices to your attendees in {country}. The law there requires invoices to go "
    "through {system}, and Revel isn't connected to it yet. Please issue invoices from your own invoicing "
    "software."
)
DOMESTIC_B2B_INVOICING_BLOCKED_MESSAGE = _(
    "Invoices to customers with a {country} VAT ID must be sent as e-invoices through {system}. Revel won't "
    "create those. Issue this one from your e-invoicing software; invoices to everyone else work as usual."
)
ONLINE_PAYMENT_BLOCKED_MESSAGE = _(
    "Online card payments aren't available for events in {country}. The law there requires paid tickets "
    "sold online to be issued by {system}, and Revel isn't approved yet. You can still sell paid tickets "
    "with payment at the door or by bank transfer, and confirm payments from your dashboard."
)
NOT_A_TAX_DOCUMENT_NOTICE = _("This ticket is not a tax invoice or receipt.")


def country_name(code: str) -> str:
    """The country's English name, translated to the active language (``Italia`` for ``IT`` in Italian)."""
    country = pycountry.countries.get(alpha_2=code) if code else None
    if country is None:
        return code
    lang = translation.get_language() or "en"
    try:
        catalog = gettext.translation(
            "iso3166-1", pycountry.LOCALES_DIR, languages=[lang.replace("-", "_"), lang.split("-")[0]]
        )
    except OSError:  # no catalog for this language (e.g. English): the source name is the answer
        return str(country.name)
    return catalog.gettext(country.name)


def format_ticket_price(amount: Decimal | int | float, currency: str) -> str:
    """Price as printed on tickets and passes: ``EUR 25.00``, or the translated "Free"."""
    if amount == 0:
        return str(_("Free"))
    return f"{currency.upper()} {Decimal(str(amount)):.2f}"


def common_ticket_fields(ticket: "Ticket") -> list[TicketComplianceField]:
    """The EU common denominator printed on every ticket (#1060, #1061, #1063, #1064).

    Event name, venue/address, date, tier and seat are already part of every ticket
    layout; these are the fiscal additions: organizer legal identity and tax ID, the
    price paid (incl. VAT) or "Free", the sequential ticket number and issue time,
    and a "not a tax document" notice.
    """
    from events.service.ticket_number_service import format_ticket_number
    from events.service.ticket_price import resolve_ticket_price
    from events.utils import format_event_datetime

    org = ticket.event.organization
    fields = [TicketComplianceField("organizer", str(_("Organizer")), org.billing_name or org.name)]
    if org.vat_id:
        fields.append(TicketComplianceField("tax_id", str(_("Tax ID")), org.vat_id))
    if number := format_ticket_number(ticket):
        fields.append(TicketComplianceField("ticket_number", str(_("Ticket no.")), number))
    if ticket.issued_at:
        issued = format_event_datetime(ticket.issued_at, ticket.event)
        fields.append(TicketComplianceField("issued_at", str(_("Issued")), issued))
    price, currency = resolve_ticket_price(ticket)
    fields.append(TicketComplianceField("price", str(_("Price")), format_ticket_price(price, currency)))
    fields.append(TicketComplianceField("notice", str(_("Tax notice")), str(NOT_A_TAX_DOCUMENT_NOTICE)))
    return fields


def in_force(nexus: frozenset[Nexus], applies_on: frozenset[Nexus], since: datetime.date | None) -> bool:
    """Whether a restriction binds this sale: a reaching nexus, and its start date has passed.

    The one generic gate every mixin uses, so any restriction can carry a scope and a
    start date without country-specific code at the call sites.
    """
    return bool(nexus & applies_on) and (since is None or timezone.localdate() >= since)


class CountryCompliancePolicy(abc.ABC):
    """The hooks every country policy answers. Call sites use nothing else.

    Every hook takes the ``nexus`` through which this country reaches the sale. The
    capabilities exposed to the frontend are derived from the hooks (for the org's own
    country, at today's date), so they can never disagree with what is enforced.

    Add a hook here, with a permissive default in :class:`DefaultEUPolicy`, when a later
    layer needs one (fiscalization provider, retention period, exports). Existing
    countries then inherit it unchanged.
    """

    def __init__(self, country: str = "") -> None:
        """Bind the policy to the resolved ISO 3166-1 country (empty when unknown)."""
        self.country = country

    @abc.abstractmethod
    def attendee_invoicing(self, buyer: BuyerContext, nexus: frozenset[Nexus]) -> Decision:
        """May Revel issue (or credit) an attendee invoice for this buyer?"""

    @abc.abstractmethod
    def online_payment(self, nexus: frozenset[Nexus]) -> Decision:
        """May a paid ticket or pass be sold through online (Stripe) checkout?"""

    @abc.abstractmethod
    def offline_payment(self, nexus: frozenset[Nexus]) -> Decision:
        """May a paid ticket be sold for offline / at-the-door payment confirmed by staff?"""

    @abc.abstractmethod
    def extra_ticket_fields(self, ticket: "Ticket", nexus: frozenset[Nexus]) -> list[TicketComplianceField]:
        """Country-specific ticket lines, printed after the EU common set (never instead of it)."""

    @t.final
    def attendee_invoicing_capability(self) -> AttendeeInvoicingCapability:
        """Invoicing as it applies to an organizer established here, today."""
        if not self.attendee_invoicing(BuyerContext(), ALL_NEXUS).allowed:
            return AttendeeInvoicingCapability.BLOCKED
        if not self.attendee_invoicing(BuyerContext(vat_country=self.country), ALL_NEXUS).allowed:
            return AttendeeInvoicingCapability.BLOCKED_FOR_BUSINESS_BUYERS
        return AttendeeInvoicingCapability.ALLOWED

    @t.final
    def online_payment_capability(self) -> PaymentChannelCapability:
        """Online checkout for events held in this country, today."""
        return _capability(self.online_payment(ALL_NEXUS))

    @t.final
    def offline_payment_capability(self) -> PaymentChannelCapability:
        """Offline / at-the-door payment for events held in this country, today."""
        return _capability(self.offline_payment(ALL_NEXUS))


def _capability(decision: Decision) -> PaymentChannelCapability:
    return PaymentChannelCapability.ALLOWED if decision.allowed else PaymentChannelCapability.BLOCKED


class DefaultEUPolicy(CountryCompliancePolicy):
    """No restriction: the policy for every country without a module of its own."""

    def attendee_invoicing(self, buyer: BuyerContext, nexus: frozenset[Nexus]) -> Decision:
        """Allowed."""
        return Decision.allow()

    def online_payment(self, nexus: frozenset[Nexus]) -> Decision:
        """Allowed."""
        return Decision.allow()

    def offline_payment(self, nexus: frozenset[Nexus]) -> Decision:
        """Allowed."""
        return Decision.allow()

    def extra_ticket_fields(self, ticket: "Ticket", nexus: frozenset[Nexus]) -> list[TicketComplianceField]:
        """None beyond the common set."""
        return []


class FiscalizedInvoicingMixin:
    """Revel's unfiscalized PDF invoices are not valid here: block attendee invoicing.

    Set ``fiscal_system``. ``fiscal_invoicing_applies_on`` says which sales the rule
    reaches (default: organizers established here) and ``fiscal_invoicing_from`` when
    it starts (default: already in force).
    """

    country: str
    fiscal_system: t.ClassVar["str | StrPromise"]
    fiscal_invoicing_applies_on: t.ClassVar[frozenset[Nexus]] = frozenset({Nexus.ESTABLISHMENT})
    fiscal_invoicing_from: t.ClassVar[datetime.date | None] = None

    def attendee_invoicing(self, buyer: BuyerContext, nexus: frozenset[Nexus]) -> Decision:
        """Blocked for every buyer, within scope and once in force."""
        if not in_force(nexus, self.fiscal_invoicing_applies_on, self.fiscal_invoicing_from):
            return Decision.allow()
        return Decision.block(
            str(ATTENDEE_INVOICING_BLOCKED_MESSAGE).format(
                country=country_name(self.country), system=self.fiscal_system
            )
        )


class DomesticB2BEInvoicingMixin:
    """Domestic B2B invoices must be structured e-invoices: block only those.

    Consumer and cross-border buyers keep Revel's PDF invoice, and only organizers
    established here are bound. Set ``e_invoicing_network`` (and optionally
    ``e_invoicing_from``).
    """

    country: str
    e_invoicing_network: t.ClassVar["str | StrPromise"]
    e_invoicing_from: t.ClassVar[datetime.date | None] = None

    def attendee_invoicing(self, buyer: BuyerContext, nexus: frozenset[Nexus]) -> Decision:
        """Blocked when an established seller invoices a buyer whose VAT ID is from here."""
        domestic_b2b = bool(buyer.vat_country) and buyer.vat_country == self.country
        if not domestic_b2b or not in_force(nexus, frozenset({Nexus.ESTABLISHMENT}), self.e_invoicing_from):
            return Decision.allow()
        return Decision.block(
            str(DOMESTIC_B2B_INVOICING_BLOCKED_MESSAGE).format(
                country=country_name(self.country), system=self.e_invoicing_network
            )
        )


class CertifiedOnlineTicketingMixin:
    """Tickets sold online must come from a certified fiscal system Revel is not: block online checkout.

    Offline and at-the-door payments confirmed by staff, free tickets, RSVPs and
    memberships are unaffected. Set ``certified_system``; the rule reaches events held
    here (``online_ticketing_applies_on``) from ``online_ticketing_from``.
    """

    country: str
    certified_system: t.ClassVar["str | StrPromise"]
    online_ticketing_applies_on: t.ClassVar[frozenset[Nexus]] = frozenset({Nexus.VENUE})
    online_ticketing_from: t.ClassVar[datetime.date | None] = None

    def online_payment(self, nexus: frozenset[Nexus]) -> Decision:
        """Blocked for events within scope, once in force."""
        if not in_force(nexus, self.online_ticketing_applies_on, self.online_ticketing_from):
            return Decision.allow()
        return Decision.block(
            str(ONLINE_PAYMENT_BLOCKED_MESSAGE).format(country=country_name(self.country), system=self.certified_system)
        )
