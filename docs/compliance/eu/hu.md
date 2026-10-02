# Hungary

Policy module: `src/events/compliance/policies/hu.py` (`HungaryPolicy`, using `FiscalizedInvoicingMixin`, plus one organizer notice).

Issue: [#1065](https://github.com/letsrevel/revel-backend/issues/1065)

## Status

**Restricted: attendee invoicing blocked for organizers established in Hungary and for physical events held there.**

Every invoice a Hungarian taxable person issues, B2C included, must be reported to NAV's Online
Számla system; invoices from invoicing software must be reported immediately and automatically. Revel
has no NAV integration, so its attendee invoices would be non-compliant.

## What Revel does

- **Attendee invoicing cannot be enabled.** Switching to HYBRID or AUTO is refused (HTTP 422) for
  organizations whose resolved country is Hungary, with a translated explanation, naming Hungary, that
  invoices must go through NAV Online Számla.
- **Generation is skipped** (invoice generation, issuing an existing draft, including drafts created
  before the gate, and credit notes) for sales by organizers established in Hungary, and for physical
  events held in Hungary by foreign organizers, because the law there reaches sellers of supplies made
  in Hungary. Virtual events held by foreign organizers are not affected. A foreign organizer can still
  enable attendee invoicing; only the sales Hungary reaches are skipped.
- **Existing settings:** organizations in Hungary that had HYBRID or AUTO were switched to NONE by the
  data migration `0128_disable_blocked_attendee_invoicing`. Existing invoices were not touched. No
  notification was sent automatically, so affected organizers should be informed out of band.
- **Organizer notice (non-blocking)**, for organizers established in Hungary and physical events held
  there (the same sales the block reaches), shown next to the attendee-invoicing setting (key `hu_nav`,
  topic `attendee_invoicing`): "Revel can't issue attendee invoices where Hungarian rules apply: invoices
  from invoicing software must be reported to NAV Online Számla in real time. If this applies to you,
  issue a receipt (nyugta) or invoice for every paid sale from your own system. Since 1 September 2026,
  data on receipts not issued by an online cash register must also be reported to NAV." A foreign
  organizer is in scope only when it becomes a Hungarian taxable person, hence "if this applies to you".
- Online and offline payments are not restricted.
- The [common ticket content](index.md#common-ticket-content) applies. Revel's ticket is not a
  receipt (*nyugta*): receipts must be in Hungarian, and the organizer still owes a receipt or invoice
  for every sale from its own system.

## Legal basis

- **Áfa tv. (Act CXXVII of 2007)**: invoice or receipt for every B2C supply (165. § (1) b),
  166. § (1)–(2)); receipt exemptions are statutory only and do not include online card payments
  (167. §); receipts in Hungarian only (178. § (4)).
  [NAV booklet no. 18 (2026-03-02)](https://nav.gov.hu/pfile/file?path=%2Fugyfeliranytu%2Fnezzen-utana%2Finf_fuz%2F2026%2F18.-A-szamla-nyugta-kibocsatasanak-alapveto-szabalyai-2026.-03.-02.)
- **Online Számla**: all invoices reported since 4 January 2021; software-issued invoices reported
  immediately, without human intervention, in XML (Annex 10).
  [NAV](https://nav.gov.hu/ado/afa/A_szamlakibocsatok_sz20201231),
  [Online Számla](https://onlineszamla.nav.gov.hu/)
- **Receipt data reporting from 1 September 2026** (257/G. §, Annex 11 Part B): daily totals by VAT
  rate within 3 calendar days for receipts not issued by an online cash register.
  [NAV](https://nav.gov.hu/ado/enyugta/nyugtaadat-szolgaltatas),
  [NAV Q&A](https://nav.gov.hu/ado/enyugta/kerdesek-es-valaszok/e-penztargep-hasznalat-adatszolgaltatas)
- **Cash register (48/2013 NGM, Annex 1)**: ticket and event admission is not listed, so no online
  cash register is required (per NAV booklet §4.2).

## Open questions

- Whether a ticket that is also a Hungarian-language receipt could let Revel help with receipts.
- The exact text of Annex 1 of 48/2013 NGM could not be fetched.
- Hungarian cultural or event-specific levies on ticket sales: none found, not researched in depth.
- Whether Online Számla reporting covers invoices of subject-exempt (AAM) organizers in the same way.

## Later layers

- An optional daily VAT-rate aggregate export for manual KOBAK entry.
- Optional NAV Online Számla v3 integration (per-organizer technical user, real-time submission,
  credit notes as modifying invoices).

See [#1065](https://github.com/letsrevel/revel-backend/issues/1065).
