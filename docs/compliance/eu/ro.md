# Romania

Policy module: `src/events/compliance/policies/ro.py` (`RomaniaPolicy`, using `FiscalizedInvoicingMixin`).

Issue: [#1064](https://github.com/letsrevel/revel-backend/issues/1064)

## Status

**Restricted: attendee invoicing blocked.**

Since 1 January 2025, B2C invoices issued by organizers established in Romania must be transmitted to
RO e-Factura. Revel's attendee invoices are PDF-only. Romania also levies a local spectacle tax with
ticket registration and ticket content rules, and a cultural stamp on ticket prices.

## What Revel does

- **Attendee invoicing cannot be enabled.** Switching to HYBRID or AUTO is refused (HTTP 422) for
  organizations whose resolved country is Romania, with a translated explanation that invoices must go
  through RO e-Factura.
- **Generation is skipped** (invoice generation, issuing an existing draft, credit notes) whenever
  Romania is a liable country of the sale: the organization is established there, or the event is a
  physical event in Romania.
- **Existing settings:** organizations in Romania that had HYBRID or AUTO were switched to NONE by the
  data migration `0128_disable_blocked_attendee_invoicing`. Existing invoices were not touched. No
  notification was sent automatically, so affected organizers should be informed out of band.
- Paid ticketing is not restricted.
- The [common ticket content](index.md#common-ticket-content) applies. It provides the organizer, its
  tax ID (when the organization's VAT ID is set), the price and a series plus sequential number, which
  HG 846/2002 asks electronic tickets to carry alongside venue, date and seat category.

Revel does not register tickets with the local tax office, produce the monthly ticket statement, or
handle the cultural stamp. These remain the organizer's obligations.

## Legal basis

- **Codul fiscal (Legea 227/2015) arts. 480–483** and **Normele metodologice (HG 1/2016), Title IX,
  pts. 156–158**: spectacle tax, ticket registration with the local tax office, price on the ticket,
  monthly payment and declaration; electronic tickets allowed from the organizer's own software.
  [ANAF consolidated text](https://static.anaf.ro/static/10/Anaf/legislatie/Cod_fiscal_norme_2023.htm)
- **HG 846/2002**: minimum ticket content, series and numbering, ticket register.
  [Portal legislativ](https://legislatie.just.ro/Public/DetaliiDocumentAfis/38202),
  [annex](https://legislatie.just.ro/Public/DetaliiDocument/38203)
- Municipal procedure (official example, Brașov), including the monthly form ITL029:
  [Brașov](https://extranet.brasovcity.ro/ServiciiDirectiaFiscalaInformatii/items/07_impozit_spectacole_PJ.html)
- **Legea 35/1994** (timbrul cultural): 3% or 5% added to the ticket price.
  [Portal legislativ](https://legislatie.just.ro/Public/DetaliiDocumentAfis/95346)
- **OUG 28/1999** (cash registers), with the show-ticket exemption in art. 2 lit. d):
  [OUG 28/1999](https://www.mfinante.gov.ro/static/10/Mfp/resurse/autorizatii/oug_28_1999.pdf),
  [ANAF Q&A, exemption](https://chat.anaf.ro/ANAFI.nsf/9ab9e3e4aaf685c6c22575e4002d7fab/e09c343acf2a42b7c2257e31002b282e?OpenDocument=),
  [ANAF Q&A, card vs transfer](https://static.anaf.ro/static/10/Anaf/AsistentaContribuabili_r/Facebook/FB_06_03_2019.pdf)
- **RO e-Factura B2C**: OUG 120/2021 art. 10¹(2) as inserted by OUG 69/2024; 5-working-day deadline
  and anonymous-buyer code from OUG 89/2025.
  [OUG 69/2024](https://static.anaf.ro/static/10/Brasov/Brasov/oug_69_e_factura.pdf),
  [OUG 89/2025](https://static.anaf.ro/static/10/Anaf/legislatie/OUG_89_2025.pdf)

## Open questions

- The full HG 846/2002 annex could not be retrieved; whether an e-ticket needs a separate stub part.
- Whether electronic-ticket number ranges must be registered per event before sale; practice varies
  by municipality.
- Current status of the cultural stamp, whether it must appear on the ticket and enter the VAT base,
  and which stamp applies to club or party events.
- Whether the cash-register exemption covers self-numbered e-tickets sold online by card.
- Whether invoices must be in Romanian; whether the spectacle tax applies to non-Romanian organizers.

## Later layers

- A spectacle-tax / ITL029 export per event and month, plus the issued number range for pre-sale
  registration.
- An optional cultural-stamp line per event or tier.
- RO e-Factura support: UBL 2.1 CIUS-RO XML next to the PDF, uploaded by the organizer or submitted by
  Revel with the organizer's authorization.

See [#1064](https://github.com/letsrevel/revel-backend/issues/1064).
