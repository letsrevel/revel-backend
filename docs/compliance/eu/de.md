# Germany

No policy module: resolves to `DefaultEUPolicy`.

Issue: [#1232](https://github.com/letsrevel/revel-backend/issues/1232)

## Status

**No restriction. Revel issues eligible attendee invoices as ZUGFeRD / Factur-X EN 16931 documents, so
German organizers invoicing through Revel meet the E-Rechnung duty for domestic B2B sales once their
transition ends (1 January 2027, or 1 January 2028 for prior-year turnover of at most 800,000 EUR).
Organizers identify themselves by a VIES-validated USt-IdNr. or, as *Kleinunternehmer* (§19 UStG),
by their *Steuernummer*
([#1244](https://github.com/letsrevel/revel-backend/issues/1244)).**

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
  - the seller snapshot has a name (BT-27), a VAT ID (BT-31) and a country (BT-40), and the buyer a
    name (BT-44) and a country (BT-55);
  - every line has a VAT category, and a reverse-charge line (AE) comes with a buyer VAT ID (BR-AE-02);
  - an out-of-scope line (O, not subject to VAT) is invoice-wide: it may not sit next to a line of
    another category (BR-O-11..14). Standard and exempt lines (S + E, with rounding) mix freely;
  - a credit note additionally has EN 16931 totals of its own.
  The XML is written only when euinvoice's totals agree with the printed ones (a one-cent VAT
  difference per rate group, which telescoped partial credit notes can produce, is resolved in favour
  of the printed figure; BR-CO-17 tolerates it) and its pre-flight passes; otherwise, and for every
  legacy, draft or otherwise ineligible document, the plain PDF of before is issued. If only the
  embedding fails, the PDF/A-3 already rendered is issued without XML (a valid PDF, no second render).
  Issuing and sending are never blocked by e-invoicing. A global switch
  (`SiteSettings.einvoice_facturx_enabled`) turns the embedding off. Already-issued documents are not
  backfilled; they gain XML only when re-rendered. Conformance is checked in CI against the official
  CEN EN 16931 rules through euinvoice.
- **Kleinunternehmer (§19 UStG) without a USt-IdNr.**
  ([#1244](https://github.com/letsrevel/revel-backend/issues/1244)). An organizer may declare the
  small-business scheme (`Organization.small_business_scheme = de_19`) and its *Steuernummer*
  (`tax_registration_number`) instead of a VIES-validated VAT ID. Turning invoicing on then requires: the
  scheme, a format-valid Steuernummer (10 or 11 digits in the Land's format, or the 13-digit
  bundeseinheitliches Format; a plausibility check only, there is no register to query), and
  establishment in Germany (the declared VAT country, else the structured billing address's country).
  The scheme means no VAT is ever charged: the organization's default rate must be 0%, no ticket tier
  may carry a positive rate, and checkout never applies reverse charge for these organizers. Declaring the
  scheme prefills the organization's exemption reason with "Steuerbefreit gemäß § 19 UStG
  (Kleinunternehmerregelung)" (editable). On every invoice and credit note each line is EN 16931 category
  E at 0% with that note (BR-E-10), the Steuernummer is printed where the VAT ID would be, and in the
  Factur-X XML it is the Seller tax registration identifier (BT-32, `schemeID="FC"`), which BR-E-02
  accepts in place of BT-31, repeated as BT-29 so BR-CO-26 holds; `vat_identifier` stays empty. §34a
  UStDV lets these organizers issue a *sonstige Rechnung* at any time, so the plain-PDF fallback stays
  legally fine. The scheme covers **domestic supplies only** (§19 Abs. 1 UStG: "im Inland"; Revel does not
  support the EU SME scheme's "-EX" registration of §19a UStG / Art. 284 of the VAT Directive), so two
  kinds of sale are **not invoiced** and are recorded as skipped fiscal documents
  ([#1091](https://github.com/letsrevel/revel-backend/issues/1091)) for the organizer to handle in its
  own software: admission to a *physical event held outside Germany*, taxed where the event takes place
  (Art. 53/54 of Directive 2006/112/EC; reason `small_business_foreign_venue`), and a *virtual event
  bought from outside Germany*, taxed where the customer is under the 2025 rules (Art. 44 for businesses,
  Art. 54(1) as amended by Directive (EU) 2022/542 for consumers; reason
  `small_business_virtual_cross_border`, EU and non-EU customers alike, a business customer would moreover
  need a reverse-charge invoice with a seller VAT ID, BR-AE-02/03). The event detail's compliance block
  reports the first as `blocked` and the second as `blocked_for_foreign_buyers`. A blank venue or buyer
  country is read as domestic, the assumption checkout prices on. Draft invoices follow the same rules:
  a line cannot be edited to carry VAT and a draft cannot be switched to reverse charge. Once invoicing is
  on, the billing settings refuse any change that would leave the seller unidentifiable (dropping the
  scheme, blanking the tax number, moving the billing country), and generation itself skips an invoice
  whose seller identity is missing (reason `seller_identity_missing`). CI validates DE and AT
  small-business fixtures against the CEN rules.
- **Not covered:** Revel cannot know an organizer's turnover, so it does not gate anything on the
  800,000 EUR threshold or on the §19 thresholds (25,000 EUR prior year, 100,000 EUR current year);
  every eligible document gets the XML, and the organizer is responsible for the scheme it declares.
  **VAT-charging organizers that identify only by their Steuernummer** (§14 Abs. 4 Nr. 2 UStG allows
  it) and hold no USt-IdNr. still cannot enable Revel invoicing: without a VIES-validated VAT ID Revel
  cannot tell a taxable person from a consumer, and the USt-IdNr. is free from the BZSt. This is a
  documented limitation, not a planned feature.
- The [common ticket content](index.md#common-ticket-content) applies. The Steuernummer is not yet
  printed on tickets (only the VAT ID is).

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
- **§19 Abs. 1 UStG** (as amended from 1 January 2025): supplies of a domestic *Kleinunternehmer* are
  *steuerfrei* when the prior-year total turnover did not exceed 25,000 EUR and the current year's does
  not exceed 100,000 EUR. Showing VAT anyway means owing it (§14c UStG).
  [§19 UStG](https://www.gesetze-im-internet.de/ustg_1980/__19.html)
- **§14 Abs. 4 Nr. 2 UStG**: an invoice carries the issuer's *Steuernummer* or its USt-IdNr.; both are
  equivalent for this purpose. The Steuernummer cannot be checked against VIES, so Revel can only check
  its format. [§14 UStG](https://www.gesetze-im-internet.de/ustg_1980/__14.html)
- **§34a UStDV**: invoices for supplies exempt under §19 Abs. 1 UStG (*Kleinunternehmer*) need only the
  names and addresses, the issuer's Steuernummer or USt-IdNr. (or Kleinunternehmer-Identifikationsnummer),
  the issue date, the supply, the consideration as one amount and a note that the small-business
  exemption applies (S. 1 Nr. 5: "dass für die Lieferung oder sonstige Leistung die Steuerbefreiung für
  Kleinunternehmer gilt"); they may always be issued as a *sonstige Rechnung*, notwithstanding §14 Abs. 2
  S. 2 UStG. [§34a UStDV](https://www.gesetze-im-internet.de/ustdv_1980/__34a.html)
- **EN 16931 (CEN 1.3.16)**: BR-E-02 requires the Seller VAT identifier (BT-31), the Seller tax
  registration identifier (BT-32) or the tax representative's VAT identifier on an invoice with exempt
  lines; BR-E-10 requires an exemption reason text or code per exempt VAT breakdown; BR-AE-02/03 require
  the seller's and the buyer's VAT identifiers for reverse charge; BR-CO-26 requires BT-29, BT-30 or
  BT-31 on the seller. There is no VATEX code for §19, so the reason is text only.
  [euinvoice country notes](https://letsrevel.github.io/euinvoice/countries/#germany)
- **Art. 18 of Implementing Regulation (EU) 282/2011**: a supplier may treat an EU customer without a
  VAT identification number as a non-taxable person. This is why Revel's own platform-fee invoice to a
  Kleinunternehmer carries Austrian VAT (see the open question below).
  [Implementing Regulation 282/2011](https://eur-lex.europa.eu/eli/reg_impl/2011/282/oj)
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
- A German VAT ID is used as a proxy for "established in Germany". Whether that is good enough. For
  a Kleinunternehmer without a VAT ID, the structured billing address's country is the proxy.
- **Platform-fee VAT for Kleinunternehmer (to confirm with the tax advisor before launch).** Revel's
  own fee invoice (Revel → organizer) is unchanged by
  [#1244](https://github.com/letsrevel/revel-backend/issues/1244): without a VIES-validated VAT ID the
  organizer is treated as a non-business customer under Art. 18 IR 282/2011, so Revel charges Austrian
  VAT instead of reverse-charging. A Kleinunternehmer cannot deduct input VAT anyway, so this does them
  no harm, but whether a German Kleinunternehmer buying a B2B service from Austria should instead be
  reverse-charged (and then owe German VAT under §13b UStG, which would also push them to get a
  USt-IdNr.) has not been confirmed. Pinned by a test so a change is deliberate.
- **Sales outside Germany by a Kleinunternehmer (to confirm with the tax advisor).** Revel does not
  invoice them (see above): a physical event abroad and a virtual event bought from abroad are gated
  rather than given an E invoice with the §19 note. Whether a German Kleinunternehmer may in practice
  rely on the EU SME scheme (§19a UStG, "-EX" suffix) for such sales, and whether Revel should then
  support that registration, is open; so is the treatment of a virtual event bought from outside the
  EU (outside EU VAT scope, category O), which the gate also covers for now.
- The Steuernummer format check accepts 10, 11 or 13 digits after stripping separators; it does not
  verify the Finanzamt prefix or a check digit.
- Ticket-specific or event-specific rules (for example local levies) were not part of this review.

## Later layers

- ZUGFeRD attendee invoices via euinvoice are in place
  ([#1235](https://github.com/letsrevel/revel-backend/issues/1235), on the EN 16931 data foundations
  of [#1234](https://github.com/letsrevel/revel-backend/issues/1234)). The fallback gate of
  [#1232](https://github.com/letsrevel/revel-backend/issues/1232) is not needed unless the duty widens.
- The Factur-X 1.0 Schematron on top of the CEN rules, once euinvoice pins it.

See [#1232](https://github.com/letsrevel/revel-backend/issues/1232).
