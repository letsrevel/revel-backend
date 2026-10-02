# Spain

Policy module: `src/events/compliance/policies/es.py` (`SpainPolicy`, using `FiscalizedInvoicingMixin`).

Issue: [#1059](https://github.com/letsrevel/revel-backend/issues/1059)

## Status

**Upcoming restriction (from 1 January 2027): attendee invoicing.**

Spain's invoicing-software rules (RRSIF, known as VERI\*FACTU) become mandatory on 1 January 2027 for
corporate-income-tax payers and 1 July 2027 for other in-scope taxpayers. When a third party issues
invoices on the seller's behalf, the third party's own system must comply. Revel's attendee invoicing
would make Revel that system, and it does not comply. Revel cannot tell whether an organizer is a
corporate taxpayer, so the earlier date applies to every organizer established in Spain. The Basque
provinces apply their own TicketBAI regimes.

## What Revel does

- **Until 31 December 2026, attendee invoicing is allowed** for organizers in Spain, as in any other
  country. The capability reported by the API reads `allowed` until then.
- **From 1 January 2027, attendee invoicing is blocked** for organizers established in Spain:
    - switching to HYBRID or AUTO is refused (HTTP 422), with a translated explanation, naming Spain,
      that invoices must go through Verifactu;
    - generation is skipped (invoice generation, issuing an existing draft, including drafts created
      before the gate, and credit notes);
    - the API capability switches to `blocked` on that date;
    - every skipped invoice and credit note is recorded for the organizer to issue from its own
      RRSIF-compliant invoicing system (VERI\*FACTU or NO VERI\*FACTU mode), including credit notes for refunds of invoices Revel issued in 2026
      ([#1091](https://github.com/letsrevel/revel-backend/issues/1091), see
      [Skipped documents](../index.md#skipped-documents)).
- **Organizer-established only.** A foreign organizer's event held in Spain is not affected.
- **Existing settings:** organizations in Spain are not switched to NONE by the data migration
  `0128_disable_blocked_attendee_invoicing`. They keep their HYBRID or AUTO setting; from 2027 the
  generation gate skips their invoices by itself, and the capability and flags report it.
- Online and offline payments are not restricted.
- The [common ticket content](index.md#common-ticket-content) applies.

## Legal basis

- **Real Decreto 1007/2023 (RRSIF)**: scope (art. 3, art. 4, including invoices issued by third
  parties), VERI\*FACTU mode (art. 16), producer certification (art. 13); deadlines in the Disposición
  final cuarta as amended by Real Decreto-ley 15/2025.
  [BOE](https://www.boe.es/buscar/act.php?id=BOE-A-2023-24840),
  [RDL 15/2025](https://www.boe.es/buscar/doc.php?id=BOE-A-2025-24446),
  [AEAT note on the extension](https://sede.agenciatributaria.gob.es/Sede/iva/sistemas-informaticos-facturacion-verifactu/nota-informativa-ampliacion-plazo-adaptacion-facturacion.html)
- **Orden HAC/1177/2024**: technical requirements (per-taxpayer record chains, SHA-256 hash chaining,
  billing records, declaración responsable, QR code and legend).
  [BOE](https://www.boe.es/buscar/act.php?id=BOE-A-2024-22138)
- **Real Decreto 1619/2012 (ROF)**: invoicing obligation (art. 2), third-party issuance needs a prior
  agreement (art. 5).
  [BOE](https://www.boe.es/buscar/act.php?id=BOE-A-2012-14696)
- **Ley 58/2003 General Tributaria**, art. 201 bis: sanctions for producing and for using
  non-compliant software.
  [BOE](https://www.boe.es/buscar/act.php?id=BOE-A-2003-23186)
- **AEAT developer FAQ v1.3**, §10: compliance falls on the system of whoever materially issues the
  invoices.
  [AEAT](https://sede.agenciatributaria.gob.es/static_files/AEAT_Desarrolladores/EEDD/IVA/VERI-FACTU/FAQs-Desarrolladores.pdf),
  [general questions](https://sede.agenciatributaria.gob.es/Sede/iva/sistemas-informaticos-facturacion-verifactu/cuestiones-generales.html)
- **TicketBAI / Batuz** (Basque Country):
  [Gipuzkoa calendar](https://www.gipuzkoa.eus/es/web/ogasuna/ticketbai/calendario),
  [Batuz FAQ](https://www.batuz.eus/es/preguntas-frecuentes)

## Open questions

- An ES VAT ID is a proxy: Canary Islands, Ceuta and Melilla sellers are outside the EU VAT area, and
  foral sellers also have ES IDs. Blocking all of Spain is the safe default.
- The exact fine for users of non-compliant software (art. 201 bis.4) could not be confirmed.
- How the producer adaptation date interacts with the RDL 15/2025 extension.
- Navarra's own regime and the full Bizkaia calendar were not researched.
- Regional *espectáculos públicos* laws: no platform-certification requirement found, not researched
  in depth.
- A ticket is not a *factura simplificada* as long as the organizer issues invoices separately.

## Later layers

- Optional: implement a VERI\*FACTU invoicing system (per-seller chained billing records, AEAT
  submission, QR and legend on the PDF, a published declaración responsable), only if there is demand.
  TicketBAI would be a separate product.
- Confirm that existing revenue exports give organizers the per-sale data their own system needs. The
  *Invoices to issue yourself* sheet lists the skipped documents with buyer, amounts and VAT
  ([#1091](https://github.com/letsrevel/revel-backend/issues/1091)).

See [#1059](https://github.com/letsrevel/revel-backend/issues/1059).
