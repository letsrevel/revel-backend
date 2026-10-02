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

A policy answers a small set of hooks. Each takes the `nexus` through which the country reaches the
sale (see [Liable countries and nexus](#liable-countries-and-nexus)):

- `attendee_invoicing(buyer, nexus)`: may Revel issue (or credit) an attendee invoice for this buyer?
- `online_payment(nexus)`: may a paid ticket or series pass be sold through online (Stripe card)
  checkout?
- `offline_payment(nexus)`: may a paid ticket be sold for offline payment (bank transfer, at the door)
  confirmed by the organizer? No country restricts this in layer 1.
- `extra_ticket_fields(ticket, nexus)`: country-specific lines printed on tickets, after the common EU
  set.
- `organizer_notices(nexus)`: non-blocking, translated hints for the organizer (`key`, `applies_to`,
  `message`). Default: none.

Countries without a module get `DefaultEUPolicy`: everything allowed, no extra ticket lines.

### How the country is resolved

An organization's country is resolved in this order:

1. The declared VAT country (`Organization.vat_country_code`), which is set by the organizer, by VIES
   validation, or from the Stripe Connect account country.
2. The two-letter prefix of the organization's VAT ID.
3. The country of the organization's city.

The VAT prefix `EL` is normalized to `GR`. If nothing is set, or the country has no module, the
organization gets `DefaultEUPolicy`.

### Liable countries and nexus

A country's rules can reach a sale in two ways, modelled by the `Nexus` enum:

- `ESTABLISHMENT`: the organizer is established there (the organization's resolved country).
- `VENUE`: a physical (non-virtual) event takes place there (the event's VAT country, from its venue
  or city).

Each restriction declares which nexus it applies on, and may carry a start date. A restriction binds a
sale only if the country reaches it through a nexus the restriction applies on, and only once the start
date has passed. A sale is refused if any reaching country refuses it.

## What layer 1 does

Layer 1 is the first step: stop Revel from producing documents that are not valid in a given country,
and print a common set of details on every ticket.

The principle is **minimal restrictions**: block strictly the non-compliant feature in each
jurisdiction, nothing broader. Only the feature, only for the sales the law reaches, only from the date
it takes effect.

- **Common ticket content.** Every ticket PDF and Apple/Google Wallet pass carries the organizer's
  legal name and tax ID, a sequential ticket number, the issue date, the price paid (or "Free") and a
  "not a tax invoice or receipt" notice. See the [EU overview](eu/index.md#common-ticket-content).
- **Attendee invoicing blocked** (HYBRID/AUTO cannot be enabled, generation is skipped) where Revel's
  PDF invoices are not valid fiscal documents:
    - for organizers established in Croatia, Portugal and Romania;
    - for organizers established in Slovenia, Greece and Hungary, and for physical events held there
      by foreign organizers;
    - in Spain, from 1 January 2027 (Verifactu), for organizers established there. Until then attendee
      invoicing is allowed.
- **B2B invoices blocked** where they must go through a national e-invoicing network, only for
  organizers established there: Belgium (Peppol) for buyers with a Belgian VAT ID, Poland (KSeF) for
  business buyers from any country. A business buyer has a VAT ID that VIES accepted or could not
  check; consumers and VIES-rejected IDs still get Revel's invoice.
- **Organizer notices, no gates**: Austria (door payments go through the organizer's own
  Registrierkasse), Denmark (covered businesses record Revel sales in their own system) and Poland
  (admission sold to consumers for discos, dance halls, amusement and theme parks, and circus performances goes through the
  organizer's own fiscal cash register, even when paid online).
- **Organizer notices with the invoicing block**: Croatia, Slovenia, Greece and Hungary also tell the
  organizer, next to the attendee-invoicing setting, that invoices go through the national system
  (fiscalization with the Porezna uprava, FURS, myDATA, NAV Online Számla) and must come from the
  organizer's own software. The notice reaches the same sales as each country's block.
- Credit notes and issuing pre-gate drafts follow the same invoice gate. A refund on a sale whose
  invoice was skipped skips its credit note too. The organizer learns of the skip through the daily
  digest below, and of the refund through
  the `TICKET_REFUNDED` notification (sent to the ticket holder and the organization's staff and
  owners) and must correct the invoice in its own system. A draft that can no longer be issued
  carries the policy's reason in `issue_blocked_reason` on the attendee-invoice responses.
- **Skipped documents are recorded** (see [below](#skipped-documents)).
- **Online payment blocked** for events held in Italy, where paid tickets sold online must be issued by
  a ticketing system approved by the Agenzia delle Entrate. Offline, bank-transfer and at-the-door
  payments confirmed by the organizer still work.
- No country restricts offline payment.
- The organization admin detail and billing-info API responses expose
  `compliance: {country, attendee_invoicing, online_payment, offline_payment}` so the frontend can hide
  what the organizer cannot use. `attendee_invoicing` is `allowed`, `blocked` or
  `blocked_for_business_buyers`; `online_payment` and `offline_payment` are `allowed` or `blocked`.
  Values are effective today: a future-dated restriction reads `allowed` until it starts. The org-level
  payment capabilities describe events held in the organization's own country.
  Both objects also carry `notices: [{key, applies_to, message}]`: non-blocking, translated hints
  (for example Austria's cash-register hint) that the frontend shows next to the setting named in
  `applies_to` (`offline_payment`: the offline / at-the-door selector; `ticket_sales`: the ticket-tier
  sales settings, for rules that cover online sales too; `attendee_invoicing`: the organization's
  attendee-invoicing mode) as information with `role="status"`. Notices never block anything.
- The event detail response (`EventDetailSchema`, used by the event admin and public event pages)
  exposes `compliance: {venue_country, online_payment, offline_payment, attendee_invoicing}` for that
  specific event: the organization's establishment plus the venue country of a physical event, at
  today's date. It is computed by `enforcement.event_compliance()` from the same decisions the tier,
  checkout and invoicing gates take, so the tier editor and checkout can hide exactly what the API would
  refuse. It is on detail responses only, not on event lists. The API still answers 422 if a refused
  sale is attempted.
- The series-pass quote (`GET /series-passes/{pass_id}/quote`) exposes `compliance: {online_payment}`:
  the decision the pass checkout gate takes for that pass at its quoted price over its upcoming
  covered events (`enforcement.series_pass_online_payment()`). `blocked` means the checkout answers 422
  once it reaches the compliance gate (buyer-eligibility or purchasability checks can answer 403 or 409
  first); an offline, free or zero-priced pass reads `allowed`. Values are effective today, as above.
- Refusal messages name the country in the user's language, for example "Online card payments aren't
  available for events in Italy. ...".

Later layers (native fiscalization, e-invoicing integrations, exports, record retention) are tracked
in the GitHub issues labelled `compliance` and linked from each country page.

### Skipped documents

Every attendee invoice or credit note a policy refuses is recorded as a `SkippedFiscalDocument`
([#1091](https://github.com/letsrevel/revel-backend/issues/1091)), snapshotted when the skip is decided:
the decision depends on today's date, billing data can change and payments are deleted with their
user, so nothing can be recomputed later. A row holds:

- `kind` (`invoice` or `credit_note`), `reason_code` (`b2b_e_invoicing` or `fiscalized_invoicing`),
  the refusing `policy_country` and its translated `reason`;
- the buyer snapshot (name, email, address, VAT ID and country, checkout VIES status), the totals,
  line items and VAT breakdown;
- the checkout session, the payments and refunds it covers, and, for a credit note, the Revel invoice
  it would correct or the skipped invoice record.

Recording is idempotent: one invoice record per checkout session, and a credit note only for refunds
no earlier record (or Revel credit note) covers. A session whose invoice was skipped stays skipped on
retries.

- `GET /organization-admin/{slug}/skipped-fiscal-documents` (owner only) lists them, filterable by
  `kind`, `reason_code`, `event_id`, `resolved` and a text `search`.
- `POST /organization-admin/{slug}/skipped-fiscal-documents/{id}/resolve` with
  `{"external_reference": "…"}` records that the organizer issued it, and under which number.
- The event ticket list (`AdminTicketSchema.invoice_skipped`, filter `invoice_skipped`) flags the
  sales, so staff who can manage tickets see them too.
- The revenue report's *Documents to issue yourself* sheet lists those decided in the period.
- A daily digest, `FISCAL_DOCUMENT_SKIPPED`
  ([#1073](https://github.com/letsrevel/revel-backend/issues/1073)), tells the owner and staff with
  `manage_tickets` about the documents skipped since the last one (beat task
  `events.notify_skipped_fiscal_documents`, 07:00 UTC). The owner's link opens the list above, the
  staff's link the flagged ticket list. It follows the digest and per-type preferences like any
  non-transactional notification.

### Known gaps

- **Ticket record retention** is not changed in layer 1. Ticket rows are still deleted (cascade)
  when the event, the tier or the user account is deleted, although France and Italy require
  ticketing records to be kept. [#1068](https://github.com/letsrevel/revel-backend/issues/1068) tracks this.
- Organizations without a VAT ID have no tax-ID line on tickets; there is no separate fiscal-code
  field yet.

## For developers

To add a country or a new hook, see [Adding a Country](adding-a-country.md).
