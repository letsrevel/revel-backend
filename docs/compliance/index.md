# Compliance

Revel sells tickets in the organizer's name. Some countries attach fiscal rules to that: certified
ticketing systems, real-time fiscalization of receipts, or mandatory e-invoicing networks. Revel
handles these with **pluggable per-country policies**. Each country with specific rules has its own
policy module; everything else falls back to a permissive default.

!!! warning "Not legal or tax advice"
    These pages describe what Revel does and summarize the research behind it. They are not legal or
    tax advice. Payments are Stripe Connect direct charges, so the **organizer is the merchant of
    record** and stays responsible for its own tax, invoicing and ticketing obligations. Organizers
    should confirm their position with a local tax advisor.

## Scope

- **EU member states** are covered country by country. See the [EU overview](eu/index.md) for the
  status of all 27.
- **Non-EU countries are out of scope for now.** They resolve to the default policy with no
  country-specific rules. Revel makes no claim about compliance in any non-EU country.

## How it works

The code lives in `src/events/compliance/`:

| Module | Role |
|---|---|
| `base.py` | The `CountryCompliancePolicy` contract, `DefaultEUPolicy` and the reusable restriction mixins. |
| `registry.py` | The `@register("XX")` decorator and the lookups `get_policy(org)` / `get_policy_for_country(code)`. |
| `policies/` | One module per country, auto-discovered at startup. |
| `enforcement.py` | Country-agnostic helpers that services call. They never check a country code themselves. |

A policy answers a small set of hooks:

- `attendee_invoicing(buyer)`: may Revel issue (or credit) an attendee invoice for this buyer?
- `paid_ticketing()`: may the organizer sell tickets that cost anything?
- `extra_ticket_fields(ticket)`: country-specific lines printed on tickets, on top of the common EU set.

Countries without a module get `DefaultEUPolicy`: everything allowed, no extra ticket lines.

### How the country is resolved

An organization's country is resolved in this order:

1. The declared VAT country (`Organization.vat_country_code`), which is set by the organizer, by VIES
   validation, or from the Stripe Connect account country.
2. The two-letter prefix of the organization's VAT ID.
3. The country of the organization's city.

The VAT prefix `EL` is normalized to `GR`. If nothing is set, or the country has no module, the
organization gets `DefaultEUPolicy`.

For invoicing and paid ticketing, the **liable countries** of a sale are the organization's resolved country and, for
physical (non-virtual) events, the event's VAT country (venue or city), because physical admission is
taxed where the event takes place. A sale is refused if any liable country refuses it.

## What layer 1 does

Layer 1 is the first, deliberately conservative step: stop Revel from producing documents that are
not valid in a given country, and print a common set of details on every ticket.

- **Common ticket content.** Every ticket PDF and Apple/Google Wallet pass carries the organizer's
  legal name and tax ID, a sequential ticket number, the issue date, the price paid (or "Free") and a
  "not a tax invoice or receipt" notice. See the [EU overview](eu/index.md#common-ticket-content).
- **Attendee invoicing blocked** (HYBRID/AUTO cannot be enabled, generation is skipped) where Revel's
  PDF invoices are not valid fiscal documents: Croatia, Spain, Portugal, Slovenia, Greece, Romania and
  Hungary.
- **Domestic B2B invoices blocked** where they must go through a national e-invoicing network:
  Belgium (Peppol) and Poland (KSeF). Consumers and cross-border buyers still get Revel's invoice.
- **Paid ticketing blocked** in Italy, where paid admission needs a certified fiscal ticketing system.
- The organization admin detail and billing-info API responses expose
  `compliance: {country, attendee_invoicing, paid_ticketing}` so the frontend can hide what the
  organizer cannot use.

Later layers (native fiscalization, e-invoicing integrations, exports, record retention) are tracked
in the GitHub issues labelled `compliance` and linked from each country page.

### Known gaps

- **Ticket record retention** is not changed in layer 1. Ticket rows are still deleted (cascade)
  when the event, the tier or the user account is deleted, although France and Italy require
  ticketing records to be kept. A follow-up issue labelled `compliance` tracks this.
- Organizations without a VAT ID have no tax-ID line on tickets; there is no separate fiscal-code
  field yet.

## For developers

To add a country or a new hook, see [Adding a Country](adding-a-country.md).
