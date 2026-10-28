# Germany

No policy module: resolves to `DefaultEUPolicy`.

Issue: [#1232](https://github.com/letsrevel/revel-backend/issues/1232)

## Status

**No restriction yet. The transition for issuing domestic B2B E-Rechnungen ends on 1 January 2027 for
larger issuers, and Revel does not handle it yet.**

Since 1 January 2025, an invoice for a supply between two businesses established in Germany must be an
*E-Rechnung*: a structured invoice conforming to EN 16931, such as XRechnung or ZUGFeRD from the EN 16931
profile up. Receiving E-Rechnungen has been mandatory since then. For **issuing**, a transition applies:
a PDF remains a valid invoice with the recipient's consent until 31 December 2026 for issuers whose
prior-year turnover is above 800,000 EUR, and until 31 December 2027 for everyone else.

Not covered:

- invoices up to 250 EUR gross (*Kleinbetragsrechnungen*);
- supplies exempt under §4 Nr. 8 to 29 UStG;
- invoices for supplies of small businesses under §19 UStG (*Kleinunternehmer*), which may always be
  issued as a *sonstige Rechnung*. This is a permanent exception, separate from the transition;
- ordinary invoices to consumers, which are outside the E-Rechnung obligation.

## What Revel does

- No country-specific restriction today: attendee invoicing and online/offline payments are allowed.
- **Gap from 1 January 2027:** when a German organizer has attendee invoicing enabled and a buyer
  with a German VAT ID places an order above 250 EUR gross, Revel issues a plain PDF on the
  organizer's behalf. For organizers above the turnover threshold, that PDF is no longer a valid
  invoice. Revel cannot know an organizer's turnover. Two options are tracked in
  [#1232](https://github.com/letsrevel/revel-backend/issues/1232): issue ZUGFeRD (Factur-X EN 16931)
  invoices via euinvoice ([#1234](https://github.com/letsrevel/revel-backend/issues/1234),
  [#1235](https://github.com/letsrevel/revel-backend/issues/1235)), or, as a fallback, gate those
  invoices as `SkippedFiscalDocument`, as for Belgium.
- The [common ticket content](index.md#common-ticket-content) applies.

## Legal basis

- **§14 Abs. 1 and 2 UStG**: definition of the E-Rechnung (EN 16931, or an agreed format convertible
  into it); duty for supplies between businesses established in Germany; supplies exempt under §4
  Nr. 8 to 29 excluded. Ordinary supplies to consumers need no E-Rechnung. The invoice duty toward
  consumers is limited to land-related work (§14 Abs. 2 UStG), which is not relevant to ticketing.
  [§14 UStG](https://www.gesetze-im-internet.de/ustg_1980/__14.html)
- **§27 Abs. 38 UStG**: transition. Issuing from 1 January 2027 for prior-year turnover above
  800,000 EUR, from 1 January 2028 for everyone else; until then PDFs with the recipient's consent.
  [§27 UStG](https://www.gesetze-im-internet.de/ustg_1980/__27.html)
- **§34a UStDV**: invoices for supplies exempt under §19 Abs. 1 UStG (*Kleinunternehmer*) may always be
  issued as a *sonstige Rechnung*, notwithstanding §14 Abs. 2 S. 2 UStG.
  [§34a UStDV](https://www.gesetze-im-internet.de/ustdv_1980/__34a.html)
- **§33 UStDV**: invoices up to 250 EUR gross (whole invoice total) may stay non-structured.
  [§33 UStDV](https://www.gesetze-im-internet.de/ustdv_1980/__33.html)
- **BMF letter of 15 October 2025** (via secondary sources): XRechnung and ZUGFeRD 2.0.1 or later
  qualify, except the MINIMUM and BASIC-WL profiles; in a hybrid PDF the XML prevails; e-mail delivery
  is enough for B2B.
  [taxpunk synopsis](https://taxpunk.de/media/e-rechnung-synopse-bmf-schreiben.pdf),
  [Haufe](https://www.haufe.de/steuern/finanzverwaltung/weiteres-bmf-schreiben-zur-e-rechnung_164_655070.html)
- **KassenSichV**: the technical security device (TSE) requirement covers electronic recording systems
  with cash functions only; online shops are out of scope. Source: AEAO zu §146a AO.
- Summary and further sources: [euinvoice country notes](https://letsrevel.github.io/euinvoice/countries/#germany)

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

- ZUGFeRD attendee invoices via euinvoice ([#1235](https://github.com/letsrevel/revel-backend/issues/1235)),
  on the EN 16931 data foundations ([#1234](https://github.com/letsrevel/revel-backend/issues/1234)).
- Fallback gate if that misses 1 January 2027 ([#1232](https://github.com/letsrevel/revel-backend/issues/1232)).

See [#1232](https://github.com/letsrevel/revel-backend/issues/1232).
