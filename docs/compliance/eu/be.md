# Belgium

Policy module: `src/events/compliance/policies/be.py` (`BelgiumPolicy`, using `DomesticB2BEInvoicingMixin`).

Issue: [#1066](https://github.com/letsrevel/revel-backend/issues/1066)

## Status

**Restricted: domestic B2B attendee invoices blocked.**

Since 1 January 2026, invoices between Belgian VAT-liable enterprises must be structured electronic
invoices (EN 16931, Peppol BIS by default) exchanged over Peppol. A PDF is no longer a valid invoice
for these transactions. B2C invoices are not covered.

## What Revel does

- **Attendee invoicing can be enabled.** HYBRID and AUTO are not refused for Belgian organizations.
- **Domestic B2B invoices are skipped.** For sales where Belgium is a liable country (the organization
  is established there, or the event is a physical event in Belgium), invoice generation, issuing an
  existing draft and credit notes are skipped only when the **buyer's VAT ID starts with `BE`**. The
  organizer must issue that invoice through Peppol with its own e-invoicing software.
- **Consumers and cross-border business buyers** still receive Revel's PDF invoice.
- Paid ticketing is not restricted.
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
- Flagging skipped sales so organizers can find them in their exports.

See [#1066](https://github.com/letsrevel/revel-backend/issues/1066).
