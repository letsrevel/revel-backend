# Austria

Policy module: `src/events/compliance/policies/at.py` (`AustriaPolicy`: no restriction, one organizer notice).

## Status

**No country-specific restrictions identified for Revel's online sales.** Payments taken at the venue
are the organizer's own cash-register matter (see below). Austrian *Kleinunternehmer* (§ 6 Abs. 1 Z 27
UStG) without a UID can invoice through Revel by declaring the scheme and their *Steuernummer*
([#1244](https://github.com/letsrevel/revel-backend/issues/1244)).

Research found no rule that requires Revel to block attendee invoicing or online or offline payments
for organizers in Austria. This is not a guarantee that organizers have no obligations of their own: as
merchant of record they remain responsible for their tax and invoicing duties.

## What Revel does

- No country-specific restriction: attendee invoicing and online/offline payments are allowed.
- **Organizer notice (non-blocking)**, for organizers established in Austria and events held there,
  shown next to the offline / at-the-door payment setting: "Payments you take at the door go through
  your own registered cash register (Registrierkasse) once you pass the legal thresholds. Revel's
  online sales are exempt."
- **Kleinunternehmer (§ 6 Abs. 1 Z 27 UStG) without a UID**
  ([#1244](https://github.com/letsrevel/revel-backend/issues/1244)). The organizer declares the scheme
  (`Organization.small_business_scheme = at_6_1_27`) and its Austrian *Steuernummer*
  (`tax_registration_number`, the nine-digit Abgabenkontonummer: two-digit Finanzamt plus seven digits,
  e.g. `12-345/6789`; format-checked only). Turning invoicing on requires the scheme, a format-valid
  Steuernummer and establishment in Austria (the declared VAT country, else the structured billing
  address's country). The scheme means no VAT is charged on anything: the default rate must be 0%, a
  tier cannot be given a positive rate and any rate a tier still carries is ignored at checkout and on
  invoices (one effective-rate choke point), and checkout never applies reverse charge. Declaring it prefills the
  exemption reason with "Umsatzsteuerbefreit – Kleinunternehmer gem. § 6 Abs. 1 Z 27 UStG" (the
  wording of the WKO's Kleinunternehmer invoice template; editable), which satisfies § 11 Abs. 1 Z 3
  lit. e. Every invoice and credit-note line is EN 16931 category E at 0% with that note; the
  Steuernummer is printed where the UID would be and goes into the Factur-X XML as the Seller tax
  registration identifier (BT-32), also as BT-29, with no BT-31. Austrian law does not require the
  Steuernummer on the invoice (see below); Revel requires it because EN 16931 BR-E-02 needs BT-31 or
  BT-32 to identify the seller. The scheme covers supplies taxable in Austria only (Revel does not
  support the EU SME scheme's "-EX" registration), so a *physical event held outside Austria* (taxed at
  the venue, Art. 53/54) and a *virtual event bought from outside Austria* (taxed where the customer is;
  a business customer would also need reverse charge) are not invoiced; they are recorded as skipped
  fiscal documents (`small_business_foreign_venue`, `small_business_virtual_cross_border`) and the event
  detail reports `blocked` / `blocked_for_foreign_buyers`; issuing a draft runs the same gates, credit
  notes never do: a credit note reverses the invoice it corrects as issued, so an invoice issued with
  VAT before the scheme was declared is credited with that VAT. Once invoicing is on, a scheme
  organizer without a VIES-validated UID (whatever UID string it may have typed) cannot make a billing
  change that would leave it unidentifiable (dropping the scheme, blanking the tax number, moving the
  billing country, deleting or replacing the UID), generation skips such an organizer's invoice
  (`seller_identity_missing`), and a draft with VAT lines or a broken identity cannot be issued; only a
  VIES-validated UID lifts that guard, and an organizer without a scheme whose validation lapsed keeps
  today's behaviour. The tier flag and the event's compliance block follow the same gates. In the
  Factur-X XML the Steuernummer alone (BT-32) carries exempt lines only (BR-E-02, not BR-S-02). CI
  validates an AT small-business fixture against the CEN rules.
- **Not covered:** VAT-charging organizers that hold only a Steuernummer and no UID still need a UID to
  invoice through Revel; without a VIES-validated VAT ID Revel cannot tell a taxable person from a
  consumer. Revel does not gate on the € 55,000 Kleinunternehmer threshold; the organizer is responsible
  for the scheme it declares.
- **Simplified invoice.** Since 1 January 2025 a Kleinunternehmer may use the simplified invoice of
  § 11 Abs. 6 UStG regardless of the amount (UStR 2000 Rz 1627): issue date, supplier name and address,
  description, date or period of supply, amount, and the exemption note in place of the rate. Revel's
  EN 16931 content (recipient, sequential number, Steuernummer) is a superset of that, which is allowed.
  **No VAT amount or rate is ever shown**: VAT shown is owed (§ 11 Abs. 12), and an issuer that is not
  the supplier owes the VAT it shows (§ 11 Abs. 14); Art. 289 of the Directive says the same.
- **Platform fee (Revel → organizer).** An Austrian Kleinunternehmer is a taxable person (§ 2, § 3a
  Abs. 5 Z 1 UStG; UStR Rz 638o), and Revel is an Austrian business, so the fee is a domestic supply
  whichever rule applies (§ 3a Abs. 6 or Abs. 7): **Austrian VAT at 20%**, no reverse charge, whether
  or not the organizer holds a UID; the Kleinunternehmer cannot deduct it. Nothing changes here with
  [#1258](https://github.com/letsrevel/revel-backend/issues/1258) (contrast
  [Germany](de.md#what-revel-does), where the fee is reverse-charged and a UID is therefore required).
  Pinned by a test.
- **Third-party issuance.** Revel issues attendee invoices and credit notes in the organizer's name
  and on its behalf (Art. 220(1) of the Directive; UStR Rz 1505, 1530–1532). Every such PDF carries the
  line "Issued by *platform legal name* in the name and on behalf of *organizer*"
  (`SiteSettings.platform_business_name`). An invoicing mandate in the organizer terms is a separate
  ToS step.
- The [common ticket content](index.md#common-ticket-content) applies. Tickets and wallet passes print
  the Steuernummer as the tax ID for an organizer with no VAT ID at all, a declared scheme and a
  complete scheme identity (scheme, establishment in Austria, format-valid Steuernummer); an organizer
  with a UID prints the UID.

## Legal basis

- **Cash-register duty (§ 131b BAO).** A business must record cash sales (*Barumsätze*) in a
  registered, signed cash register (RKSV) once its turnover exceeds €15,000 a year and its cash sales
  exceed €7,500 a year. *Barumsätze* include payments **by debit or credit card** and comparable
  electronic payments, not just cash. So card payments are not excluded as such.
  [§ 131b BAO (RIS)](https://www.ris.bka.gv.at/NormDokument.wxe?Abfrage=Bundesnormen&Gesetzesnummer=10003940&Paragraf=131b)
- **Online-platform exemption (§ 6 Barumsatzverordnung 2015).** Sales are exempt from the
  cash-register duty when both conditions hold: (1) the customer does not pay the business in cash
  directly, and (2) the sale rests on an agreement concluded through an online platform. Tickets bought
  through Revel's online checkout (Stripe) meet both conditions.
  [§ 6 BarUV 2015 (RIS)](https://www.ris.bka.gv.at/NormDokument.wxe?Abfrage=Bundesnormen&Gesetzesnummer=20009267&FassungVom=2024-10-27&Paragraf=6),
  [USP: Registrierkassenpflicht](https://www.usp.gv.at/themen/steuern-finanzen/steuerliche-rechte-und-pflichten/registrierkassen.html)
- BMF guidance confirms that online card payments that are not made on site are not *Barumsätze*,
  while card payments at the business premises are.
  [BMF, Vereine und Registrierkassenpflicht](https://www.bmf.gv.at/themen/steuern/spenden-gemeinnuetzigkeit/haeufig-gestellte-fragen-zu-vereinen-gemeinn%C3%BCtzigkeit-und-registrierkassenpflicht.html)
- **Payments at the venue are different.** Money taken at the door or on site (cash or card) for a
  Revel ticket with the *at the door* or *offline* payment method is a *Barumsatz* of the organizer.
  Above the thresholds, the organizer must record it in their own RKSV cash register and give a
  receipt. Revel does not handle that money and is not a registered cash register. Bank transfers are
  not *Barumsätze*.

- **Kleinunternehmer exemption (§ 6 Abs. 1 Z 27 UStG 1994).** The turnover of a *Kleinunternehmer* (a
  business operated in Austria, or in another member state under the EU-wide scheme) is exempt once its
  turnover under § 1 Abs. 1 Z 1 and 2 stays within the Kleinunternehmergrenze of € 55,000 in the
  previous and the current calendar year (version in force from 2025; the RIS text consulted is the one
  from 1 January 2026). The exemption is *unecht*: no input-tax deduction (§ 12 Abs. 3). VAT shown on an
  invoice anyway is owed (§ 11 Abs. 12).
  [§ 6 UStG 1994 (RIS)](https://www.ris.bka.gv.at/NormDokument.wxe?Abfrage=Bundesnormen&Gesetzesnummer=10004873&Paragraf=6)
- **Invoice content (§ 11 UStG 1994).** § 11 Abs. 1 Z 3 lit. e requires, for an exempt supply, a note
  that an exemption applies ("Hinweis, dass für diese Lieferung oder sonstige Leistung eine
  Steuerbefreiung gilt"); lit. i requires the supplier's UID only for supplies that carry the right to
  input-tax deduction, which exempt small-business supplies do not. § 11 does not mention the
  Steuernummer at all, and Abs. 6 lets a Kleinunternehmer use the small-amount invoice content (as for
  totals up to € 400). So the Steuernummer on a Revel invoice is an EN 16931 requirement (BR-E-02), not
  an Austrian one. [§ 11 UStG 1994 (RIS)](https://www.ris.bka.gv.at/NormDokument.wxe?Abfrage=Bundesnormen&Gesetzesnummer=10004873&Paragraf=11),
  [WKO Kleinunternehmer invoice template](https://www.wko.at/wien/steuern/rechnung-kleinunternehmen-muster.docx)
- **Simplified invoice (§ 11 Abs. 6 UStG 1994, idF BGBl. I Nr. 97/2025).** "Bei Rechnungen ... die von
  einem Unternehmer ausgestellt werden, der die Steuerbefreiung in § 6 Abs. 1 Z 27 in Anspruch nimmt",
  the small-amount content suffices; UStR 2000 Rz 1627: Kleinunternehmer "können ab 1.1.2025 die
  vereinfachte Rechnungslegung ... unabhängig vom in der Rechnung ausgewiesenen Betrag anwenden". Not
  available for supplies in another member state or reverse-charge cases (Abs. 6 last sentences), which
  are exactly the sales Revel refuses for scheme organizers.
  [UStR Rz 1625 ff. (Findok)](https://findok.bmf.gv.at/findok/volltext?segmentId=0b135b32-0383-45f8-9f60-c7c8dedb2adc)
- **No VAT amount on a Kleinunternehmer invoice.** § 11 Abs. 12 (tax shown without being owed is owed
  on the strength of the invoice) and § 11 Abs. 14 (an issuer that is not the supplier owes the tax it
  shows); Art. 289 of Directive 2006/112/EC.
- **Platform fee (Revel → organizer).** A Kleinunternehmer is an Unternehmer (§ 2 Abs. 1 UStG) and
  counts as a business recipient for the place-of-supply rules (§ 3a Abs. 5 Z 1; UStR Rz 638o). Revel's
  fee to an Austrian organizer is supplied in Austria either way (§ 3a Abs. 6 for a business, Abs. 7
  otherwise): Austrian VAT at 20%, no reverse charge. For a *German* Kleinunternehmer the same
  reasoning moves the place of supply to Germany with reverse charge (Art. 44/196), which is why the
  German scheme requires a VIES-validated USt-IdNr. ([de.md](de.md)).
  [UStR Rz 638k ff. (Findok)](https://findok.bmf.gv.at/findok/volltext?segmentId=14a019d2-0500-4139-ac29-a35960bd891d)
- **Third-party issuance.** Art. 220(1) of Directive 2006/112/EC ("in his name and on his behalf, by a
  third party"); UStR Rz 1505 ("Der zur Abrechnung ... Verpflichtete kann sich im Abrechnungsverfahren
  dritter Personen bedienen") and Rz 1530–1532 (invoicing "im Namen und für Rechnung" of the supplier:
  the supplier's name, address and, where it exists, UID on the document). Not a *Gutschrift* (§ 11
  Abs. 7: self-billing by the recipient). Rz 1531 / § 11 Abs. 14: the issuer owes VAT it shows without
  authority, hence the hard no-VAT rule for scheme organizers.
  [UStR Rz 1505 ff. (Findok)](https://findok.bmf.gv.at/findok/volltext?segmentId=fc2306a0-c9dc-4515-b0f6-c7f5ce7584ec),
  [UStR Rz 1527 ff. (Findok)](https://findok.bmf.gv.at/findok/volltext?segmentId=756c063d-6f4b-4aff-807e-d4980c6e2a68)

## Open questions

- **For the Steuerberater** ([#1258](https://github.com/letsrevel/revel-backend/issues/1258)): whether
  Revel's platform fee is an electronically supplied service (Art. 7 IR 282/2011) and Revel's position
  against the EUR 10,000 threshold of Art. 3a Abs. 5 BMR (EU-OSS); the Art. 9a IR 282/2011
  deemed-supplier risk should on-demand virtual content ever be sold; and the ZM effect of the German
  UID requirement. None of these changes the Austrian case above. Details in
  [de.md](de.md#open-questions).
- The Steuernummer format check only verifies nine digits after stripping separators; it does not
  verify the Finanzamt number or the check digit. Sources differ on whether the last seven digits are
  read as six plus a check digit; this does not affect the check.
- **Sales outside Austria by a Kleinunternehmer.** Gated, not invoiced (see above); the research of
  30 October 2026 confirmed the gate as reasonable: a physical event abroad is taxed at the venue
  (§ 3a Abs. 11 lit. a / Abs. 11a; Art. 53/54) and the home exemption covers domestic supplies only
  (Art. 284(1)); a virtual event is taxed where the customer is (B2C EU from the first euro, § 3a Abs. 13
  lit. b; B2B EU reverse charge, category AE; non-EU not taxable here, category O), so the home note is
  wrong for all of them. Supporting the EU SME scheme ("-EX" registration, Art. 6a BMR) would need a
  verified EX number and the venue state's invoice rules and stays out of scope.
- **Door sales recorded in Revel.** When an organizer marks an *at the door* or *offline* ticket as
  paid in Revel after taking cash or card on site, that payment still has to go through the
  organizer's RKSV cash register (above the thresholds). Revel has no obligation of its own here, so
  there is no gate; the organizer notice above tells them. Whether a deeper integration (e.g. exporting
  door sales to a Registrierkasse) is worth building is open.
- Relief for non-profit associations (*begünstigte Körperschaften*, e.g. small club events) and the
  outdoor-sales rule may exempt some organizers; not assessed per organizer.
- Ticket-specific or event-specific rules (for example local levies) were not part of this review.

## Later layers

None planned. No issue is open for Austria.
