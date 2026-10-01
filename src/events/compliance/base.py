"""The country-policy contract, the EU default, and the reusable restriction mixins.

A country module subclasses :class:`DefaultEUPolicy` (usually through one of the
mixins below) and registers itself with :func:`events.compliance.registry.register`.
Call sites only ever talk to the resolved policy object.
"""

import abc
import enum
import typing as t
from dataclasses import dataclass
from decimal import Decimal

from django.utils.translation import gettext_lazy as _

if t.TYPE_CHECKING:
    from events.models import Ticket


class AttendeeInvoicingCapability(enum.StrEnum):
    """Whether Revel may issue attendee invoices in the organizer's name."""

    ALLOWED = "allowed"
    BLOCKED = "blocked"
    # Domestic B2B invoices must go through a national e-invoicing network.
    BLOCKED_FOR_BUSINESS_BUYERS = "blocked_for_business_buyers"


class PaidTicketingCapability(enum.StrEnum):
    """Whether the organizer may sell paid tickets through Revel."""

    ALLOWED = "allowed"
    BLOCKED = "blocked"


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
    "Revel cannot issue attendee invoices for organizers in {country}: invoices there must go through "
    "{system}. Please issue them from your own compliant invoicing software."
)
DOMESTIC_B2B_INVOICING_BLOCKED_MESSAGE = _(
    "Revel cannot issue this invoice: invoices between businesses in {country} must be exchanged as "
    "structured e-invoices via {system}. Please issue it from your own e-invoicing software."
)
PAID_TICKETING_BLOCKED_MESSAGE = _(
    "Paid tickets are not available for organizers in {country}: tickets for paid events there must be "
    "issued by {system}. Free tickets and RSVPs remain available."
)
NOT_A_TAX_DOCUMENT_NOTICE = _("This ticket is not a tax invoice or receipt.")


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


class CountryCompliancePolicy(abc.ABC):
    """The hooks every country policy answers. Call sites use nothing else.

    Capabilities are class-level facts (exposed to the frontend); the decision hooks
    may refine them per call. Add a hook here — with a permissive default in
    :class:`DefaultEUPolicy` — when a later layer needs one (fiscalization provider,
    retention period, exports); existing countries then inherit it unchanged.
    """

    attendee_invoicing_capability: t.ClassVar[AttendeeInvoicingCapability]
    paid_ticketing_capability: t.ClassVar[PaidTicketingCapability]

    def __init__(self, country: str = "") -> None:
        """Bind the policy to the resolved ISO 3166-1 country (empty when unknown)."""
        self.country = country

    @abc.abstractmethod
    def attendee_invoicing(self, buyer: BuyerContext) -> Decision:
        """May Revel issue (or credit) an attendee invoice for this buyer?"""

    @abc.abstractmethod
    def paid_ticketing(self) -> Decision:
        """May the organizer sell tickets that cost anything?"""

    @abc.abstractmethod
    def extra_ticket_fields(self, ticket: "Ticket") -> list[TicketComplianceField]:
        """Country-specific ticket lines, appended after the common set."""

    @t.final
    def ticket_fields(self, ticket: "Ticket") -> list[TicketComplianceField]:
        """Every compliance line for a ticket: the EU common set, then the country's additions.

        Final on purpose: a country can add lines, never drop the common ones.
        """
        return [*common_ticket_fields(ticket), *self.extra_ticket_fields(ticket)]


class DefaultEUPolicy(CountryCompliancePolicy):
    """No restriction: the policy for every country without a module of its own."""

    attendee_invoicing_capability: t.ClassVar[AttendeeInvoicingCapability] = AttendeeInvoicingCapability.ALLOWED
    paid_ticketing_capability: t.ClassVar[PaidTicketingCapability] = PaidTicketingCapability.ALLOWED

    def attendee_invoicing(self, buyer: BuyerContext) -> Decision:
        """Allowed."""
        return Decision.allow()

    def paid_ticketing(self) -> Decision:
        """Allowed."""
        return Decision.allow()

    def extra_ticket_fields(self, ticket: "Ticket") -> list[TicketComplianceField]:
        """None beyond the common set."""
        return []


class FiscalizedInvoicingMixin:
    """Revel's PDF invoices are not valid here: block attendee invoicing outright.

    Set ``fiscal_system`` to the national system the organizer must use instead.
    """

    country: str
    fiscal_system: t.ClassVar[str]
    attendee_invoicing_capability: t.ClassVar[AttendeeInvoicingCapability] = AttendeeInvoicingCapability.BLOCKED

    def attendee_invoicing(self, buyer: BuyerContext) -> Decision:
        """Blocked for every buyer."""
        return Decision.block(
            str(ATTENDEE_INVOICING_BLOCKED_MESSAGE).format(country=self.country, system=self.fiscal_system)
        )


class DomesticB2BEInvoicingMixin:
    """Domestic B2B invoices must be structured e-invoices: block only those.

    Consumer and cross-border buyers keep Revel's PDF invoice. Set ``e_invoicing_network``.
    """

    country: str
    e_invoicing_network: t.ClassVar[str]
    attendee_invoicing_capability: t.ClassVar[AttendeeInvoicingCapability] = (
        AttendeeInvoicingCapability.BLOCKED_FOR_BUSINESS_BUYERS
    )

    def attendee_invoicing(self, buyer: BuyerContext) -> Decision:
        """Blocked when the buyer's VAT ID is from this country."""
        if buyer.vat_country and buyer.vat_country == self.country:
            return Decision.block(
                str(DOMESTIC_B2B_INVOICING_BLOCKED_MESSAGE).format(
                    country=self.country, system=self.e_invoicing_network
                )
            )
        return Decision.allow()


class CertifiedTicketingMixin:
    """Paid admission needs a certified fiscal ticketing system Revel is not: block paid tickets.

    Set ``certified_system``. Free tickets, RSVPs and memberships are unaffected.
    """

    country: str
    certified_system: t.ClassVar[str]
    paid_ticketing_capability: t.ClassVar[PaidTicketingCapability] = PaidTicketingCapability.BLOCKED

    def paid_ticketing(self) -> Decision:
        """Blocked."""
        return Decision.block(
            str(PAID_TICKETING_BLOCKED_MESSAGE).format(country=self.country, system=self.certified_system)
        )
