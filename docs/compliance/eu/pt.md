# Portugal

Policy module: `src/events/compliance/policies/pt.py` (`PortugalPolicy`, using `FiscalizedInvoicingMixin`).

Issue: [#1060](https://github.com/letsrevel/revel-backend/issues/1060)

## Status

**Restricted: attendee invoicing blocked.**

A Portuguese taxable person that issues invoices with software must use only software certified in
advance by the Autoridade Tributária e Aduaneira (AT). Revel is not AT-certified, and its PDFs lack the
mandatory ATCUD and AT QR code.

## What Revel does

- **Attendee invoicing cannot be enabled.** Switching to HYBRID or AUTO is refused (HTTP 422) for
  organizations whose resolved country is Portugal, with a translated explanation that invoices must
  go through AT-certified invoicing software.
- **Generation is skipped** (invoice generation, issuing an existing draft, credit notes) whenever
  Portugal is a liable country of the sale: the organization is established there, or the event is a
  physical event in Portugal.
- **Existing settings:** organizations in Portugal that had HYBRID or AUTO were switched to NONE by
  the data migration `0128_disable_blocked_attendee_invoicing`. Existing invoices were not touched. No
  notification was sent automatically, so affected organizers should be informed out of band.
- Paid ticketing is not restricted.
- The [common ticket content](index.md#common-ticket-content) applies. It covers the ticket details
  DL 23/2014 art. 8 asks for: the promoter's identity and tax ID (when the organization's VAT ID is
  set), the price, and a sequential number, alongside the event, venue, date and seat category.

## Legal basis

- **DL 28/2019**: certified software is mandatory if any one condition is met, including using
  invoicing software (art. 4(1)); third-party invoice preparation leaves the organizer liable
  (art. 5(1)); online tickets do not qualify for the ticket carve-out (art. 4(6)); ATCUD and QR code
  on every fiscally relevant document (art. 7(3)).
  [AT consolidated text](https://info.portaldasfinancas.gov.pt/pt/informacao_fiscal/legislacao/diplomas_legislativos/Documents/Decreto_Lei_28_2019.pdf),
  [DR](https://diariodarepublica.pt/dr/detalhe/decreto-lei/28-2019-119622094),
  [AT FAQ](https://info.portaldasfinancas.gov.pt/pt/apoio_ao_contribuinte/Negocios/Faturacao/Regras_de_faturacao/Documents/FAQ_DL28_2019_2019_10_01.pdf)
- **CIVA** arts. 29, 36 and 40: invoice obligation, deadlines and content; a ticket can replace the
  invoice but is then a fiscal document.
  [art. 29](https://info.portaldasfinancas.gov.pt/pt/informacao_fiscal/codigos_tributarios/civa_rep/Pages/iva29.aspx),
  [art. 36](https://info.portaldasfinancas.gov.pt/pt/informacao_fiscal/codigos_tributarios/civa_rep/Pages/iva36.aspx),
  [art. 40](https://info.portaldasfinancas.gov.pt/pt/informacao_fiscal/codigos_tributarios/civa_rep/Pages/iva40.aspx)
- **Portaria 195/2020** (ATCUD and QR), **Portaria 363/2010** (certification),
  **Despacho 8632/2014** (technical requirements):
  [Portaria 195/2020](https://info.portaldasfinancas.gov.pt/pt/informacao_fiscal/legislacao/diplomas_legislativos/Documents/Portaria_195_2020.pdf),
  [Portaria 363/2010](https://info.portaldasfinancas.gov.pt/pt/informacao_fiscal/legislacao/diplomas_legislativos/Documents/Portaria_363_2010.pdf),
  [Despacho 8632/2014](https://info.portaldasfinancas.gov.pt/pt/informacao_fiscal/legislacao/diplomas_legislativos/Documents/Despacho_n%C2%BA_8632_2014_03_07.pdf)
- **e-Fatura reporting** (DL 198/2012):
  [AT FAQ](https://info.portaldasfinancas.gov.pt/pt/apoio_contribuinte/questoes_frequentes/pages/faqs-00978.aspx),
  [DL 198/2012](https://info.portaldasfinancas.gov.pt/pt/informacao_fiscal/legislacao/diplomas_legislativos/Documents/Decreto-Lei%20n%20_198_2012_24_08.pdf)
- **DL 23/2014** art. 8(1): ticket content for artistic shows; promoter registration with IGAC.
  [DR 2014](https://files.diariodarepublica.pt/1s/2014/02/03200/0137901389.pdf),
  [IGAC](https://www.igac.gov.pt/espetaculos/espetaculos-de-natureza-artistica)
- **DL 65/2026**: companion tickets (public-sector venues only) and WCAG 2.1 AA for ticketing platforms.
  [DR](https://diariodarepublica.pt/dr/detalhe/decreto-lei/65-2026-1066993459),
  [Government communiqué](https://portugal.gov.pt/gc25/comunicacao/noticias/pessoas-com-deficiencia-reforco-de-direitos-com-nova-estrategia-e-bilhete-gratuito-para-acompanhante)
- [AT list of certified programs](https://www.portaldasfinancas.gov.pt/pt/Out/consultaProgCertificadosM24.action)

## Open questions

- Whether any de-minimis exception applies to small associations (the CIVA art. 53 threshold is
  unverified).
- Non-resident organizers VAT-registered in Portugal are also covered; the trigger is a PT VAT
  obligation, not only a seat in Portugal.
- Whether a foreign producer without a PT tax number can file for certification; the exact QR spec and
  test procedure.
- The consolidated DL 23/2014 text (with later amendments) and the scope of DL 125/2003 are unverified.

## Later layers

- A per-order export so organizers can key or import invoices into their own certified software, and
  optionally an integration with a certified invoicing provider chosen by the organizer.
- Only if the market justifies it: Revel becoming an AT-certified producer (signed hash chains,
  ATCUD and QR, SAF-T export, e-Fatura).

See [#1060](https://github.com/letsrevel/revel-backend/issues/1060).
