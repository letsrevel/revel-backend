# Croatia

Policy module: `src/events/compliance/policies/hr.py` (`CroatiaPolicy`, using `FiscalizedInvoicingMixin`).

Issue: [#1058](https://github.com/letsrevel/revel-backend/issues/1058)

## Status

**Restricted: attendee invoicing blocked for organizers established in Croatia.**

Since 1 January 2026 every B2C invoice (*račun*) issued by a Croatian profit-tax or self-employed
income-tax payer must be fiscalized in real time with the Tax Administration (Porezna uprava),
whatever the payment method, including online card payments. Revel's attendee invoices are not
fiscalized.

## What Revel does

- **Attendee invoicing cannot be enabled.** Switching to HYBRID or AUTO is refused (HTTP 422) for
  organizations whose resolved country is Croatia, with a translated explanation, naming Croatia, that
  invoices must go through the Tax Administration's fiscalization system.
- **Generation is skipped** (invoice generation, issuing an existing draft, including drafts created
  before the gate, and credit notes) for sales by organizers established in Croatia. A foreign
  organizer's event held in Croatia is not affected.
- **Existing settings:** organizations in Croatia that had HYBRID or AUTO were switched to NONE by the
  data migration `0128_disable_blocked_attendee_invoicing`. Existing invoices were not touched. No
  notification was sent automatically, so affected organizers should be informed out of band.
- Online and offline payments are not restricted.
- The [common ticket content](index.md#common-ticket-content) applies, including the "not a tax
  invoice or receipt" notice.

## Legal basis

- **Zakon o fiskalizaciji, NN 89/2025**: fiscalization of B2C invoices paid by any method (čl. 3);
  event tickets are not among the exemptions (čl. 4); receipt content with JIR, ZKI and QR code
  (čl. 7); three-part invoice numbering (čl. 9); signing with the obligor's certificate (čl. 10);
  real-time submission (čl. 15); software must not allow changes after issue, and its producer is
  co-responsible (čl. 14); documents issued before the fiscalized invoice must state "OVO NIJE
  FISKALIZIRANI RAČUN" (čl. 29).
  [Narodne novine](https://narodne-novine.nn.hr/clanci/sluzbeni/full/2025_06_89_1233.html)
- **Pravilnik o fiskalizaciji računa u krajnjoj potrošnji, NN 153/2025**: certificate, ZKI algorithm,
  QR code, transport.
  [Narodne novine](https://narodne-novine.nn.hr/clanci/sluzbeni/full/2025_12_153_2278.html)
- Porezna uprava FAQs:
  [B2C fiscalization](https://porezna-uprava.gov.hr/hr/fiskalizacija-racuna-u-krajnjoj-potrosnji-b2c-poslovanje/8033),
  [eRačun and fiscalization, non-profit associations, authorizations](https://porezna-uprava.gov.hr/hr/izdavanje-i-primanje-eracuna-i-fiskalizacija-eracuna/8047)
- [Tehnička specifikacija za korisnike v2.6](https://porezna-uprava.gov.hr/UserDocsImages/Fiskalizacija/Tehni%C4%8Dke%20specifikacije/Fiskalizacija%20-%20Tehnicka%20specifikacija%20za%20korisnike_v2.6.pdf)

The organizer, as merchant of record, is the fiscalization obligor. Associations are obligors only
when they are profit-tax payers.

## Open questions

- Whether a foreign platform can fiscalize with its own certificate under a FiskAplikacija
  authorization, or must use the organizer's certificate.
- The deadline for issuing the fiscalized receipt after an online card payment (a "48h webshop rule"
  is unverified).
- Whether a given association is a profit-tax payer is decided case by case.
- Whether receipts must be in Croatian; whether any ticket-specific rules exist.
- Whether price-showing documents (Stripe receipts, confirmation emails) need the čl. 29 statement.
- Which payment-method code ("kartica" or "ostalo") applies to Stripe card and wallet payments.

## Later layers

- Organizer guidance for Croatia and a revenue/VAT export that organizers can feed into their own
  fiscalization software.
- Optional native fiscalization: per-organization certificate storage, Croatian numbering, ZKI/JIR
  fields on invoices and credit notes, a signed SOAP submission task with retries, and fiscal data on
  the PDF.

See [#1058](https://github.com/letsrevel/revel-backend/issues/1058).
