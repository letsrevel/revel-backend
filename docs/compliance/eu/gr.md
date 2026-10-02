# Greece

Policy module: `src/events/compliance/policies/gr.py` (`GreecePolicy`, using `FiscalizedInvoicingMixin`, plus one organizer notice).

Issue: [#1063](https://github.com/letsrevel/revel-backend/issues/1063)

## Status

**Restricted: attendee invoicing blocked for organizers established in Greece and for physical events held there.**

Greek-established organizers must document every B2C ticket sale with a Greek retail document whose
data is transmitted to AADE's myDATA platform, and documents issued from software must carry a myDATA
QR code. Revel's attendee invoices are not transmitted to myDATA. The VAT prefix `EL` resolves to
Greece.

## What Revel does

- **Attendee invoicing cannot be enabled.** Switching to HYBRID or AUTO is refused (HTTP 422) for
  organizations whose resolved country is Greece, with a translated explanation, naming Greece, that
  invoices must go through myDATA.
- **Generation is skipped** (invoice generation, issuing an existing draft, including drafts created
  before the gate, and credit notes) for sales by organizers established in Greece, and for physical
  events held in Greece by foreign organizers, because the law there reaches sellers of supplies made
  in Greece. Virtual events held by foreign organizers are not affected. A foreign organizer can still
  enable attendee invoicing; only the sales Greece reaches are skipped.
- **Existing settings:** organizations in Greece that had HYBRID or AUTO were switched to NONE by the
  data migration `0128_disable_blocked_attendee_invoicing`. Existing invoices were not touched. No
  notification was sent automatically, so affected organizers should be informed out of band.
- **Organizer notice (non-blocking)**, for organizers established in Greece and physical events held
  there (the same sales the block reaches), shown next to the attendee-invoicing setting (key
  `gr_mydata`, topic `attendee_invoicing`): "Revel can't issue attendee invoices where Greek rules apply:
  receipts and invoices must be transmitted to AADE's myDATA. If you must issue Greek documents, issue
  them from your own software, a certified e-invoicing provider or AADE's free tools (timologio,
  myDATAapp). Invoices to businesses must go through a provider or AADE's tools." Conditional,
  because whether Law 4308/2014 reaches non-Greek organizers is an open question (below).
- Online and offline payments are not restricted.
- The [common ticket content](index.md#common-ticket-content) applies, including the "not a tax
  invoice or receipt" notice.

## Legal basis

- **A.1138/2020**, as replaced by **A.1170/2023**: myDATA transmission of retail documents (types
  11.1/11.2, refunds 11.4) and e-shop order notes; QR code on ERP-issued documents from 1.1.2024.
  [A.1138/2020](http://elib.aade.gr/elib/DesktopModules/ViewModule/Documents/gr-ap-2020-A__1138-A__1138_2020.pdf),
  [A.1170/2023](http://elib.aade.gr/elib/DesktopModules/ViewModule/Documents/gr-ap-2023-A__1170-A__1170_2023.pdf)
- **A.1155/2023** (card terminal to cash system linking), with the e-commerce and payment-link
  exclusions added by **A.1074/2024**: Revel's card-not-present online sales appear out of scope.
  [A.1155/2023](http://elib.aade.gr/elib/DesktopModules/ViewModule/Documents/gr-ap-2023-A__1155-A__1155_2023.pdf),
  [A.1074/2024](http://elib.aade.gr/elib/DesktopModules/ViewModule/Documents/gr-ap-2024-A__1074-A__1074_2024.pdf)
- **A.1112/2025**: obligations of certified e-invoicing providers (optional route for organizers).
  [A.1112/2025](http://elib.aade.gr/elib/DesktopModules/ViewModule/Documents/gr-ap-2025-A__1112-A__1112_2025.pdf)
- **VAT Directive 2006/112/EC** art. 53: admission to an event in Greece is taxed in Greece.
  [EUR-Lex](https://eur-lex.europa.eu/eli/dir/2006/112/oj)

The organizer can meet its obligation with its own ERP, a certified provider, or AADE's free tools
(timologio, myDATAapp). B2B invoices are mandatory e-invoices since 2 March 2026 for businesses with 2023
gross income over €1M (joint decision A.1044/2026, ΦΕΚ Β΄880/17-02-2026, which moved the start from
2 February and allowed a gradual phase-in until 3 May 2026), and from 1 October 2026 for everyone else.
They must go through a certified provider or AADE's tools, so an own ERP alone no longer suffices for them.
[A.1044/2026](https://www.taxheaven.gr/circulars/52245/a-1044-2026)

## Open questions

- Whether **A.1160/2025** narrowed the e-commerce exclusion in A.1155/2023; the text could not be read.
  [A.1160/2025](https://www.aade.gr/sites/default/files/2025-11/a1160_2025fek.pdf)
- Transmission timing for retail documents issued via ERP.
- Whether the cash-register exemption categories of ΠΟΛ.1002/2014 cover online ticket sales.
- Whether Law 4308/2014 applies to non-Greek organizers with a Greek VAT registration.
- Ticket-specific rules (municipal levies, nominal tickets, Ministry of Culture e-ticketing) were not
  found in official sources.

## Later layers

- A per-order sales export so organizers can issue retail documents in their own tool.
- Optional myDATA integration, either Revel calling the myDATA API as the organizer's ERP or posting
  to a certified provider chosen by the organizer, with MARK and QR code on the PDF.

See [#1063](https://github.com/letsrevel/revel-backend/issues/1063).
