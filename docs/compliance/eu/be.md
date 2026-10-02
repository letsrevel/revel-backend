# Belgium

Policy module: `src/events/compliance/policies/be.py` (`BelgiumPolicy`, using `B2BEInvoicingMixin` with the default `b2b_buyer_scope = DOMESTIC`).

Issue: [#1066](https://github.com/letsrevel/revel-backend/issues/1066)

## Status

**Restricted: domestic B2B attendee invoices blocked for organizers established in Belgium.**

Since 1 January 2026, invoices between Belgian VAT-liable enterprises must be structured electronic
invoices (EN 16931, Peppol BIS by default) exchanged over Peppol. A PDF is no longer a valid invoice
for these transactions. B2C invoices are not covered.

## What Revel does

- **Attendee invoicing can be enabled.** HYBRID and AUTO are not refused for Belgian organizations.
- **Domestic B2B invoices are skipped.** For organizers established in Belgium, invoice generation,
  issuing an existing draft (including drafts created before the gate) and credit notes are skipped
  only when the **buyer's VAT ID starts with `BE`** and VIES confirmed it as valid at checkout or
  could not be reached. A VAT ID that VIES rejected (a typo, a made-up number) makes the buyer a
  consumer, who gets Revel's invoice as usual. The organizer must issue that invoice through
  Peppol with its own e-invoicing software.
- **Consumers and cross-border business buyers** still receive Revel's PDF invoice.
- **Skipped documents are recorded** ([#1091](https://github.com/letsrevel/revel-backend/issues/1091)).
  Each skipped invoice or credit note is stored with the buyer, amounts, VAT breakdown and the
  policy's reason. The owner sees them under *Documents to issue yourself*
  (`GET /organization-admin/{slug}/skipped-fiscal-documents`) and marks each one done with the number
  from its own system (`POST …/{id}/resolve`). The ticket list flags those sales (`invoice_skipped`),
  and the revenue report has an *Documents to issue yourself* sheet.
- **Foreign organizers' events held in Belgium are not affected**: the Peppol mandate excludes
  suppliers not established in Belgium.
- Online and offline payments are not restricted.
- The [common ticket content](index.md#common-ticket-content) applies.

## Legal basis

- **Law of 6 February 2024** and **VAT Code art. 53, §2bis**: structured e-invoicing between Belgian
  VAT-liable enterprises for invoices issued from 1 January 2026. B2C is excluded; small enterprises
  under the franchise scheme are not.
  [FAQ](https://einvoice.belgium.be/en/FAQ/general-questions-b2b),
  [mandate](https://einvoice.belgium.be/en/article/structured-electronic-invoices-between-companies-are-compulsory-2026),
  [when it applies](https://einvoice.belgium.be/en/article/when-e-invoicing-mandatory)
- **Registered cash register (GKS)**: horeca only, not relevant to online ticketing.
  [FOD Financiën](https://financien.belgium.be/nl/Actueel/nieuwe-toepassingsmodaliteiten-van-de-het-geregistreerd-kassasysteem)

## Open questions

- Penalty amounts and any start-of-2026 tolerance period were not found on official pages.
- The royal decree implementing art. 53 §2bis was not retrieved.
- Edge cases: Belgian non-profits registered for VAT only for intra-EU acquisitions, or partially
  liable.
- Whether domestic B2B ticket sales are common enough to justify a full Peppol integration.
- The 2013 anti-touting law restricts resale above face value; not re-verified.

## Later layers

- Full Peppol support: EN 16931 / Peppol BIS Billing 3.0 UBL invoices and credit notes sent through a
  Peppol Access Point, with participant lookup and delivery tracking.
- ~~Flagging skipped sales so organizers can find them in their exports.~~ Done in
  [#1091](https://github.com/letsrevel/revel-backend/issues/1091).

See [#1066](https://github.com/letsrevel/revel-backend/issues/1066).
