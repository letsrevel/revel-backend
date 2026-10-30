# Germany

No policy module: resolves to `DefaultEUPolicy`.

Issue: [#1232](https://github.com/letsrevel/revel-backend/issues/1232)

## Status

**No restriction. Revel issues eligible attendee invoices as ZUGFeRD / Factur-X EN 16931 documents, so
German organizers invoicing through Revel meet the E-Rechnung duty for domestic B2B sales once their
transition ends (1 January 2027, or 1 January 2028 for prior-year turnover of at most 800,000 EUR).
Organizers identify themselves by a VIES-validated USt-IdNr.; *Kleinunternehmer* (§19 UStG) declare
the scheme on top of it and may add their *Steuernummer*
([#1244](https://github.com/letsrevel/revel-backend/issues/1244),
[#1258](https://github.com/letsrevel/revel-backend/issues/1258)).**

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
- **Kleinunternehmer (§19 UStG)**
  ([#1244](https://github.com/letsrevel/revel-backend/issues/1244),
  [#1258](https://github.com/letsrevel/revel-backend/issues/1258)). An organizer declares the
  small-business scheme (`Organization.small_business_scheme = de_19`). Turning invoicing on then
  requires: the scheme, a **VIES-validated USt-IdNr.** and establishment in Germany (the declared VAT
  country, else the structured billing address's country). The *Steuernummer* (`tax_registration_number`)
  is optional; when given it must be format-valid (10 or 11 digits in the Land's format, or the 13-digit
  bundeseinheitliches Format; a plausibility check only, there is no register to query). **Why the
  USt-IdNr. is required:** a Kleinunternehmer is a taxable person, so Revel's own platform fee to it is a
  B2B service supplied in Germany with reverse charge (Art. 44 and 196 of the VAT Directive, §13b UStG on
  the German side); the reverse-charge invoice and Revel's Zusammenfassende Meldung need the recipient's
  USt-IdNr., and Revel cannot fall back on treating the organizer as a consumer once it has declared
  itself a business (Art. 18(2) of Implementing Regulation 282/2011). The USt-IdNr. is free from the
  BZSt (§27a UStG). The 422 messages on turning invoicing on, on the billing PATCH and on the VAT-ID
  endpoints say so. The scheme means no VAT is ever charged: the organization's default rate must be 0%,
  a tier cannot be given a positive rate and any rate a tier still carries (for example one copied by
  event duplication) is ignored at checkout and on invoices (one effective-rate choke point), and
  checkout never applies reverse charge for these organizers. Declaring the scheme prefills the
  organization's exemption reason with "Steuerbefreit gemäß § 19 UStG (Kleinunternehmerregelung)"
  (editable). On every invoice and credit note each line is EN 16931 category E at 0% with that note
  (BR-E-10), **no VAT amount or rate is ever shown** (§14c UStG: VAT shown is VAT owed): the PDF of an
  invoice or credit note generated for a scheme organizer (`AttendeeInvoice.seller_small_business_scheme`,
  snapshotted at generation) has no VAT column, no per-line rate and no Subtotal/VAT totals, only the
  gross amounts, the Total and the note (the Factur-X XML keeps its category-E breakdown at 0, which EN
  16931 needs structurally; that is a breakdown, not a VAT amount shown), the USt-IdNr.
  is printed as the seller's identifier (§34a S. 1 Nr. 2 UStDV accepts the Steuernummer or the
  USt-IdNr.), and in the Factur-X XML it is the Seller VAT identifier (BT-31) with the Steuernummer, when
  set, as the Seller tax registration identifier (BT-32, `schemeID="FC"`). §34a UStDV lets these
  organizers issue a *sonstige Rechnung* at any time, so the plain-PDF fallback stays legally fine. The
  scheme covers **domestic supplies only** (§19 Abs. 1 UStG: "im Inland"; Revel does not
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
  a line cannot be edited to carry VAT, a draft cannot be switched to reverse charge, and issuing a draft
  runs the two gates. Credit notes do not: a credit note reverses the invoice it corrects as issued (an
  invoice issued with VAT before the scheme was declared is credited with that VAT), whatever the
  organizer's scheme or the event's venue have become since. The German scheme is declared and kept
  only with a VIES-validated USt-IdNr., whatever the invoicing mode: a billing PATCH that would leave
  `de_19` without one is refused (422 with the message above), and so is deleting the USt-IdNr. (leave
  the scheme first); the Steuernummer may be blanked or changed freely. Replacing the USt-IdNr. is
  decided on the actual VIES outcome, so an organizer whose monthly revalidation lapsed recovers by
  resubmitting a valid USt-IdNr.: valid → stored and validated; invalid → 422 and nothing is written;
  VIES down → 503 and nothing is written. While a lapse is unresolved, generation skips the
  organizer's invoices (reason `seller_identity_missing`), a draft cannot be issued, and the platform
  fee for that period falls back to Austrian VAT (Art. 18 IR 282/2011); a draft that still carries VAT
  lines cannot be issued either. An organizer without a scheme whose validation lapsed keeps today's behaviour and is still
  invoiced with its stored VAT ID. The tier flag `invoicing_available` and the event's compliance block
  (`blocked`) follow the same gates, so checkout never promises an invoice generation would skip. A
  credit note on a scheme-issued invoice stays exempt even where it has to fall back to the payment (a
  line replaced by hand in DRAFT). In the revenue report, 0% sales of a scheme organizer (or of an exempt
  tier) form their own "0%" bucket; "reverse charge" is reserved for sales checkout actually
  reverse-charged. CI validates DE (USt-IdNr. plus Steuernummer) and AT (Steuernummer only)
  small-business fixtures against the CEN rules.
- **Platform fee (Revel → organizer).** Revel is an Austrian business. Its fee to a German organizer
  with a VIES-validated USt-IdNr., a Kleinunternehmer included, is a B2B service supplied in Germany and
  **reverse-charged** (Art. 44 and 196 of the VAT Directive): no Austrian VAT, the organizer accounts for
  German VAT under §13b UStG (a Kleinunternehmer cannot deduct it; §19 Abs. 1 S. 2 UStG does not exclude
  §13b). A German organizer *without* a validated USt-IdNr. is treated as a non-taxable customer under
  Art. 18 of Implementing Regulation 282/2011 and charged Austrian VAT, as before; a scheme organizer
  is in that position only while a VIES revalidation lapse is unresolved, since the scheme cannot be
  declared or kept without a validated USt-IdNr. Pinned by tests so a change is deliberate.
- **Third-party issuance.** Revel issues attendee invoices and credit notes in the organizer's name
  and on its behalf, which Art. 220(1) of the VAT Directive and §14 Abs. 2 S. 7 UStG allow. Every such
  PDF carries the line "Issued by *platform legal name* in the name and on behalf of *organizer*", and
  the Factur-X XML the same sentence as the invoice note (BT-22), so the document is unambiguously the
  organizer's. The legal name (`SiteSettings.platform_business_name`) is snapshotted on the invoice
  and on the credit note when each is created (`issuer_name`); documents from before the snapshot
  name the current operator. An invoicing mandate in the organizer terms is a separate ToS step.
- **Not covered:** Revel cannot know an organizer's turnover, so it does not gate anything on the
  800,000 EUR threshold or on the §19 thresholds (25,000 EUR prior year, 100,000 EUR current year);
  every eligible document gets the XML, and the organizer is responsible for the scheme it declares.
  **VAT-charging organizers that identify only by their Steuernummer** (§14 Abs. 4 Nr. 2 UStG allows
  it) and hold no USt-IdNr. still cannot enable Revel invoicing: without a VIES-validated VAT ID Revel
  cannot tell a taxable person from a consumer, and the USt-IdNr. is free from the BZSt. This is a
  documented limitation, not a planned feature.
- The [common ticket content](index.md#common-ticket-content) applies. Tickets and wallet passes print
  the organization's VAT ID when one is set (for a scheme organizer: once VIES-validated, as on its
  invoices), so a German Kleinunternehmer's tickets carry its USt-IdNr. and print no tax ID while a
  validation lapse is unresolved. The Steuernummer is printed only for an organizer with no VAT ID at
  all, a declared scheme and a
  complete scheme identity, which after [#1258](https://github.com/letsrevel/revel-backend/issues/1258)
  is the Austrian scheme only (the German scheme's identity is never complete without the USt-IdNr.).

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
- **Platform fee: a Kleinunternehmer is a business customer.** AT § 2 Abs. 1 and § 3a Abs. 5 Z 1 UStG
  1994 (UStR 2000 Rz 638o: "Unecht befreite Unternehmer (zB Kleinunternehmer) ... fallen unter den
  Unternehmerbegriff") make the organizer a taxable person for place-of-supply purposes; **Art. 44 and
  196** of Directive 2006/112/EC (§ 3a Abs. 6 UStG 1994; **§13b UStG** on the German side, which
  §19 Abs. 1 S. 2 UStG does not switch off) put Revel's fee in Germany with the organizer owing the VAT.
  **Art. 18(2) of Implementing Regulation (EU) 282/2011** lets a supplier treat an EU customer without a
  VAT number as a non-taxable person only "unless he has information to the contrary"; the second
  subparagraph ("irrespective of information to the contrary") covers electronically supplied services
  only and would then place the supply in Germany too (§ 3a Abs. 13 lit. a). The reverse-charge invoice
  needs the recipient's VAT ID (§ 11 Abs. 1a UStG 1994) and so does the ZM (Art. 21 Abs. 6 Z 3 BMR);
  **§27a Abs. 1 UStG** grants the USt-IdNr. to any business on request, free of charge.
  [Implementing Regulation 282/2011](https://eur-lex.europa.eu/eli/reg_impl/2011/282/oj),
  [§13b UStG](https://www.gesetze-im-internet.de/ustg_1980/__13b.html),
  [§27a UStG](https://www.gesetze-im-internet.de/ustg_1980/__27a.html)
- **Third-party issuance.** Art. 220(1) of Directive 2006/112/EC: an invoice is issued by the supplier,
  by the customer or, "in his name and on his behalf, by a third party"; **§14 Abs. 2 S. 7 UStG**
  (AT UStR Rz 1505, 1530–1532). This is not a *Gutschrift* (self-billing by the recipient), so §34a S. 1
  Nr. 6 UStDV's "Gutschrift" mention does not apply. The issuer's exposure is a VAT amount shown
  without authority (AT § 11 Abs. 14 UStG 1994; VwGH 22.2.2000, 99/14/0062), which the scheme's
  no-VAT rule removes.
- **No VAT amount, ever.** §14c UStG (VAT shown is VAT owed), Art. 289 of the Directive (small
  enterprises "may not show the VAT on their invoices").
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
- A German VAT ID is used as a proxy for "established in Germany". Whether that is good enough. (An
  Austrian Kleinunternehmer without a VAT ID is placed by its structured billing address's country.)
- **For the Steuerberater** (research of 30 October 2026,
  [#1258](https://github.com/letsrevel/revel-backend/issues/1258)):
    - whether Revel's platform fee is an *electronically supplied service* (Art. 7 IR 282/2011) and
      where Revel's EU-wide B2C e-service turnover stands against the EUR 10,000 threshold of Art. 3a
      Abs. 5 BMR / Art. 59c of the Directive. This decides whether any "no USt-IdNr." fallback may carry
      Austrian VAT at all, or needs German VAT via EU-OSS. Revel's answer today is to require the
      USt-IdNr. and reverse-charge, so no fallback exists;
    - the effect of the required USt-IdNr. on Revel's own filings (ZM, Art. 21 Abs. 3 BMR: quarterly, the
      VAT ID of each organizer);
    - **Art. 9a IR 282/2011** (deemed supplier): if Revel ever sells on-demand or pre-recorded virtual
      content, an e-service sold through a platform, Revel itself could be presumed the supplier
      regardless of the organizer's scheme. Live interactive streams are outside Art. 9a. Virtual
      events should stay live-only until this is advised on;
    - the German-law points (§13b for Kleinunternehmer recipients, §19 Abs. 4 / §19a, §34a UStDV, §14
      Abs. 2 S. 7) come from gesetze-im-internet.de, which is not on the research allowlist; a German
      adviser should confirm them.
- **Sales outside Germany by a Kleinunternehmer.** Revel does not invoice them (see above). The
  research of 30 October 2026 confirmed the gate as reasonable: admission to a physical event abroad is
  taxed at the venue (Art. 53/54; §3a Abs. 3 Nr. 3 and 5 UStG) and §19 Abs. 1 UStG exempts domestic
  supplies only ("im Inland"; Art. 284(1) of the Directive), so foreign VAT is due unless the organizer
  holds a "-EX" number confirmed for the venue state (§19 Abs. 4 / §19a UStG); a virtual event is taxed
  where the customer is (EU consumer: from the first euro, the EUR 10,000 threshold covers e-services
  only; EU business: reverse charge, category AE, with a ZM; non-EU: category O), so the §19 note is wrong
  for all of them. Supporting "-EX" (a verified EX number plus the venue state's invoice rules) stays out
  of scope.
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
