# Spain

Policy module: `src/events/compliance/policies/es.py`:

- `SpainPolicy` (`ES`), using `FiscalizedInvoicingMixin`, plus one organizer notice until the block starts;
- `BasqueCountryPolicy` (`ES-PV`), TicketBAI, in force now
  ([Basque Country](#basque-country-ticketbai));
- `NavarrePolicy` (`ES-NC`), Spain's 2027 block with Navarre's own wording ([Navarre](#navarre)).

Issues: [#1059](https://github.com/letsrevel/revel-backend/issues/1059),
[#1086](https://github.com/letsrevel/revel-backend/issues/1086)

## Status

**Upcoming restriction (from 1 January 2027): attendee invoicing.**

Spain's invoicing-software rules (RRSIF, known as VERI\*FACTU) become mandatory on 1 January 2027 for
corporate-income-tax payers and 1 July 2027 for other in-scope taxpayers. When a third party issues
invoices on the seller's behalf, the third party's own system must comply. Revel's attendee invoicing
would make Revel that system, and it does not comply. Revel cannot tell whether an organizer is a
corporate taxpayer, so the earlier date applies to every organizer established in Spain.

**In force now in the Basque Country: attendee invoicing blocked (TicketBAI).** The foral territories
are outside VERI\*FACTU and run their own systems; see [Basque Country](#basque-country-ticketbai)
and [Navarre](#navarre).

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
- **Organizer notice (non-blocking), until 31 December 2026**, for organizers established in Spain,
  shown next to the attendee-invoicing setting (key `es_verifactu`, topic `attendee_invoicing`):
  "From 1 January 2027, Revel stops issuing attendee invoices for organizers in Spain, because it can't
  meet Spain's invoicing-software rules (Verifactu), which start applying in 2027. If you use attendee
  invoicing, set up your own invoicing software before then." Organizers in the Basque Country don't
  get it (they are blocked already), and organizers in Navarre get their own wording (see below).
  It is gated on the policy's `fiscal_invoicing_from`, so it disappears on the day the block starts;
  from then on the block's own explanation takes over. It shows whatever the organization's invoicing
  mode, so organizers who might switch invoicing on also learn the date.
- **Existing settings:** organizations in Spain are not switched to NONE by the data migration
  `0128_disable_blocked_attendee_invoicing`. They keep their HYBRID or AUTO setting; from 2027 the
  generation gate skips their invoices by itself, and the capability and flags report it.
- Online and offline payments are not restricted.
- The [common ticket content](index.md#common-ticket-content) applies.

## Basque Country (TicketBAI)

Organizers whose tax domicile is in Álava, Bizkaia or Gipuzkoa are under the foral TicketBAI regimes,
not VERI\*FACTU (RD 1007/2023, art. 1.3). TicketBAI is mandatory today in all three provinces: Álava
since 1 December 2022, Gipuzkoa since 1 June 2023, and Bizkaia (where it is part of Batuz) since
1 January 2026. It also covers invoices a third party issues in the taxpayer's name: outsourcing the
invoicing "en ningún caso le exime". A Revel PDF has no TBAI code or QR and is not sent to the foral
treasury, so it is non-compliant **today**.

- **Detection:** the organization resolves to `ES` and its city is in Spain with `City.admin_name`
  "Basque Country" (also accepted: "País Vasco", "Pais Vasco", "Euskadi", any case). Its jurisdiction
  is then `ES-PV`. A city elsewhere never overrides the declared country (an FR VAT ID with a Bilbao
  city stays `FR`). There is no override field: the city is the signal. What actually decides it is
  the Concierto Económico (Ley 12/2002): tax domicile in the Basque Country, with the €12M / 75%
  exception for large companies, which Revel can't see.
- **What Revel does:** attendee invoicing is blocked from today, for organizers established there
  only (a foreign or Madrid organizer's event in Bilbao is unaffected):
    - switching to HYBRID or AUTO is refused (HTTP 422): "Revel can't issue invoices to your attendees
      in the Basque Country. The law there requires invoices to go through TicketBAI (Batuz in
      Bizkaia), and Revel isn't connected to it. Please issue invoices from your own
      TicketBAI-compliant invoicing software.";
    - generation, issuing drafts and credit notes are skipped and recorded under *Documents to issue
      yourself* with `reason_code: fiscalized_invoicing` and `policy_country: "ES"`;
    - the org `compliance` object reads `country: "ES"`, `region: "ES-PV"`,
      `attendee_invoicing: "blocked"`, and carries no `es_verifactu` notice.
- **Existing data:** no Basque organizations existed when this shipped, so no data migration or
  organizer notification was needed.

## Navarre

Navarre-domiciled taxpayers are also outside VERI\*FACTU (RD 1007/2023, art. 1.3). The Hacienda
Foral de Navarra has announced its own system, NaTicket, without a start date. Revel keeps Spain's
2027 block for Navarre (jurisdiction `ES-NC`, detected like the Basque Country from `admin_name`
"Navarre", "Navarra" or "Nafarroa") and only corrects the wording so it doesn't claim Verifactu:

- notice until 31 December 2026 (key `es_nc_naticket`): "From 1 January 2027, Revel stops issuing
  attendee invoices for organizers in Spain, Navarre included. Navarre is bringing in its own
  invoicing-software rules (NaTicket), and Revel won't be connected to them. If you use attendee
  invoicing, set up your own invoicing software before then."
- refusal from 1 January 2027: "Revel doesn't issue invoices to attendees for organizers in Spain,
  Navarre included. Navarre is bringing in its own invoicing-software rules (NaTicket), and Revel
  isn't connected to them. Please issue invoices from your own invoicing software."

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
- **Real Decreto 1007/2023, art. 1.3**: the RRSIF does not apply to taxpayers under the foral
  regimes of the Basque Country and Navarre.
  [BOE](https://www.boe.es/buscar/act.php?id=BOE-A-2023-24840)
- **Ley 12/2002 (Concierto Económico)**: which taxpayers fall under the Basque foral treasuries.
  [BOE](https://www.boe.es/buscar/act.php?id=BOE-A-2002-10260)
- **TicketBAI / Batuz** (Basque Country):
  [Álava FAQ 1.9, start dates](https://web.araba.eus/es/-/ticketbai/faq/1-9),
  [Álava, who is affected](https://web.araba.eus/es/hacienda/ticketbai/a-quien-afecta),
  [Gipuzkoa calendar](https://www.gipuzkoa.eus/es/web/ogasuna/ticketbai/calendario),
  [Batuz FAQ](https://www.batuz.eus/es/preguntas-frecuentes) (Q38: invoices issued by third parties)
- **NaTicket** (Navarre): announced by the Hacienda Foral de Navarra, no start date yet.

## Open questions

- An ES VAT ID is a proxy: Canary Islands, Ceuta and Melilla sellers are outside the EU VAT area, and
  foral sellers also have ES IDs. Blocking all of Spain is the safe default.
- The exact fine for users of non-compliant software (art. 201 bis.4) could not be confirmed.
- How the producer adaptation date interacts with the RDL 15/2025 extension.
- The Basque Country is detected from the organization's city, a proxy for tax domicile. An
  organizer with a Basque city but a common-territory domicile (or a large company under the
  Concierto's volume rule) is blocked anyway; one without a city is treated as common territory.
- An invoice whose organization was deleted keeps only its seller country (`ES`), so it is judged by
  the common-territory rule, not TicketBAI.
- When NaTicket gets a date, Navarre's block may need to move.
- Regional *espectáculos públicos* laws: no platform-certification requirement found, not researched
  in depth.
- A ticket is not a *factura simplificada* as long as the organizer issues invoices separately.

## Later layers

- Optional: implement a VERI\*FACTU invoicing system (per-seller chained billing records, AEAT
  submission, QR and legend on the PDF, a published declaración responsable), only if there is demand.
  TicketBAI would be a separate product.
- Confirm that existing revenue exports give organizers the per-sale data their own system needs. The
  *Documents to issue yourself* sheet lists the skipped documents with buyer, amounts and VAT
  ([#1091](https://github.com/letsrevel/revel-backend/issues/1091)).

See [#1059](https://github.com/letsrevel/revel-backend/issues/1059).
