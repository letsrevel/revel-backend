# Germany

No policy module: resolves to `DefaultEUPolicy`.

Issue: [#1232](https://github.com/letsrevel/revel-backend/issues/1232)

## Status

**No restriction. Revel issues eligible attendee invoices as ZUGFeRD / Factur-X EN 16931 documents, so
German organizers meet the E-Rechnung duty for domestic B2B sales once the transition ends on
1 January 2027.**

Since 1 January 2025, an invoice for a supply between two businesses established in Germany must be an
*E-Rechnung*: a structured invoice conforming to EN 16931, such as XRechnung or ZUGFeRD from the EN 16931
profile up. Receiving E-Rechnungen has been mandatory since then. For **issuing**, a transition applies.
Both the supply date and the date the invoice is sent count:

- supplies in 2025 and 2026: paper, or another electronic format (such as a PDF) with the recipient's
  consent, if the invoice is **sent by 31 December 2026**;
- supplies in 2027: the same, if the invoice is **sent by 31 December 2027** and the issuer's prior-year
  total turnover (*Gesamtumsatz*, §19 Abs. 2 UStG) did not exceed 800,000 EUR. A separate 2027 route
  for EDI (with consent) covers only electronic formats, never paper, and does not apply to Revel's
  e-mailed PDFs.

So for issuers above 800,000 EUR, a PDF for a supply made in 2027 is no longer a valid B2B invoice.

Not covered:

- invoices up to 250 EUR gross (*Kleinbetragsrechnungen*), except for supplies under §§3c, 6a and 13b
  UStG (§33 S. 3 UStDV);
- supplies exempt under §4 Nr. 8 to 29 UStG;
- invoices for supplies of small businesses under §19 UStG (*Kleinunternehmer*), which may always be
  issued as a *sonstige Rechnung*. This is a permanent exception, separate from the transition;
- ordinary invoices to consumers, which are outside the E-Rechnung obligation.

## What Revel does

- No country-specific restriction: attendee invoicing and online/offline payments are allowed.
- **ZUGFeRD / Factur-X EN 16931 attendee invoices**
  ([#1235](https://github.com/letsrevel/revel-backend/issues/1235)). An eligible issued attendee
  invoice or credit note is stored as a PDF/A-3 with the EN 16931 CII XML embedded (Factur-X
  conformance level `EN 16931`, ZUGFeRD "COMFORT"), which the BMF accepts as an E-Rechnung; the XML
  prevails over the visual PDF. The document is eligible when all of the following hold:
  - it is issued (drafts carry a watermark and never get XML) with EN 16931 totals
    ([#1234](https://github.com/letsrevel/revel-backend/issues/1234));
  - the seller snapshot has a VAT ID (BT-31) and a country (BT-40), and the buyer a country (BT-55);
  - every line has a VAT category, and a reverse-charge line (AE) comes with a buyer VAT ID (BR-AE-02);
  - an out-of-scope line (O, not subject to VAT) is invoice-wide: it may not sit next to a line of
    another category (BR-O-11..14). Standard and exempt lines (S + E, with rounding) mix freely;
  - a credit note additionally has EN 16931 totals of its own.
  The XML is written only when euinvoice's totals agree with the printed ones (a one-cent VAT
  difference per rate group, which telescoped partial credit notes can produce, is resolved in favour
  of the printed figure; BR-CO-17 tolerates it) and its pre-flight passes; otherwise, and for every legacy, draft or otherwise ineligible document, the plain PDF of
  before is issued. Issuing and sending are never blocked by e-invoicing. A global switch
  (`SiteSettings.einvoice_facturx_enabled`) turns the embedding off. Already-issued documents are not
  backfilled; they gain XML only when re-rendered. Conformance is checked in CI against the official
  CEN EN 16931 rules through euinvoice.
- **Not covered:** Revel cannot know an organizer's turnover, so it does not gate anything on the
  800,000 EUR threshold; every eligible document gets the XML. Organizers without an EU VAT ID
  (*Kleinunternehmer*, [#1244](https://github.com/letsrevel/revel-backend/issues/1244)) get the plain
  PDF, which §34a UStDV allows them.
- The [common ticket content](index.md#common-ticket-content) applies.

## Legal basis

- **§14 Abs. 1 and 2 UStG**: definition of the E-Rechnung (EN 16931, or an agreed format convertible
  into it); duty for supplies between businesses established in Germany; supplies exempt under §4
  Nr. 8 to 29 excluded. Ordinary supplies to consumers need no E-Rechnung. The invoice duty toward
  consumers is limited to land-related work (§14 Abs. 2 UStG), which is not relevant to ticketing.
  [§14 UStG](https://www.gesetze-im-internet.de/ustg_1980/__14.html)
- **§27 Abs. 38 UStG**: transition. Nr. 1: supplies in 2025–2026, invoice sent by 31 December 2026,
  paper or another electronic format with consent. Nr. 2: supplies in 2027, invoice sent by
  31 December 2027, prior-year *Gesamtumsatz* (§19 Abs. 2) up to 800,000 EUR. Nr. 3: supplies in 2027,
  a non-EN 16931 electronic format exchanged via EDI with consent (no paper).
  [§27 UStG](https://www.gesetze-im-internet.de/ustg_1980/__27.html)
- **§34a UStDV**: invoices for supplies exempt under §19 Abs. 1 UStG (*Kleinunternehmer*) may always be
  issued as a *sonstige Rechnung*, notwithstanding §14 Abs. 2 S. 2 UStG.
  [§34a UStDV](https://www.gesetze-im-internet.de/ustdv_1980/__34a.html)
- **§33 UStDV**: invoices up to 250 EUR gross (whole invoice total) may always be issued as a *sonstige
  Rechnung*; S. 3 excludes supplies under §§3c, 6a and 13b UStG.
  [§33 UStDV](https://www.gesetze-im-internet.de/ustdv_1980/__33.html)
- **BMF letter of 15 October 2025** (via secondary sources): XRechnung and ZUGFeRD 2.0.1 or later
  qualify, except the MINIMUM and BASIC-WL profiles; in a hybrid PDF the XML prevails; e-mail delivery
  is enough for B2B.
  [taxpunk synopsis](https://taxpunk.de/media/e-rechnung-synopse-bmf-schreiben.pdf),
  [Haufe](https://www.haufe.de/steuern/finanzverwaltung/weiteres-bmf-schreiben-zur-e-rechnung_164_655070.html)
- **KassenSichV**: the technical security device (TSE) requirement covers electronic recording systems
  with cash functions only; online shops are out of scope. Source: AEAO zu §146a AO.
- Summary and further sources: [euinvoice country notes](https://letsrevel.github.io/euinvoice/countries/#germany)
- What the Factur-X EN 16931 profile validates against, and the Factur-X Schematron still to be
  pinned: [euinvoice compliance](https://letsrevel.github.io/euinvoice/compliance/)

## Open questions

- The primary text of the BMF letter was not retrieved (the BMF site returned errors on 28 October
  2026); its content comes from secondary sources.
- KassenSichV and Revel's at-the-door and offline payments recorded by staff: the AEAO test is about
  recording systems with a cash function, which can include comparable on-site payments. Pure online
  sales are out of scope. Whether staff-recorded door payments bring an organizer's setup into scope
  has not been assessed.
- **§4 Nr. 20 UStG** exempts some cultural events (theatres, concerts and similar by public bodies or
  certified equivalents), whose invoices need no E-Rechnung. Whether to model it or leave it to the
  organizer.
- A German VAT ID is used as a proxy for "established in Germany". Whether that is good enough.
- Ticket-specific or event-specific rules (for example local levies) were not part of this review.

## Later layers

- ZUGFeRD attendee invoices via euinvoice are in place
  ([#1235](https://github.com/letsrevel/revel-backend/issues/1235), on the EN 16931 data foundations
  of [#1234](https://github.com/letsrevel/revel-backend/issues/1234)). The fallback gate of
  [#1232](https://github.com/letsrevel/revel-backend/issues/1232) is not needed unless the duty widens.
- The Factur-X 1.0 Schematron on top of the CEN rules, once euinvoice pins it.

See [#1232](https://github.com/letsrevel/revel-backend/issues/1232).
