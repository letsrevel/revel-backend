# EU

This page describes the baseline that applies to organizers in every EU member state, and the status
of each country. Country pages add what is specific to that country.

## Default policy

A member state without its own policy module resolves to `DefaultEUPolicy`: everything is allowed
(attendee invoicing, online payment and offline payment) and no extra ticket lines are added. The
common ticket content below still applies.

Country modules restrict only the non-compliant feature, only for the sales that country's law reaches
(organizers established there, physical events held there, or both), and only from the date the rule
takes effect. No country restricts offline payment.

## Common ticket content

Every ticket PDF and Apple/Google Wallet pass carries the following, whatever the country:

- **Organizer**: the organization's billing name, or its display name if no billing name is set.
- **Tax ID**: the organization's VAT ID, when one is set.
- **Ticket number**: a sequential number per organization, in the form `SERIES-000123`.
    - The series is derived from the organization's slug when its first ticket is numbered, and never
      changes afterwards.
    - The number is assigned atomically when the ticket is first issued (it becomes ACTIVE or
      CHECKED_IN), never while it is pending.
    - Numbers are assigned without gaps and never reused, and cancelled tickets keep theirs. Holes
      appear only if numbered tickets are later deleted, because tickets are still removed when their
      event, tier or user account is deleted ([#1068](https://github.com/letsrevel/revel-backend/issues/1068)).
    - In Portugal, Revel reads the per-organization counter as meeting DL 23/2014's sequential
      numbering, pending any contrary view from a Portuguese lawyer or IGAC ([pt.md](pt.md)).
- **Issued**: the issue date and time.
- **Price**: the price paid including VAT (for example `EUR 25.00`), or "Free" for zero-price tickets.
- **Tax notice**: "This ticket is not a tax invoice or receipt." The ticket emails to the buyer
  (ticket created, ticket updated, payment confirmation) carry the same line
  ([#1085](https://github.com/letsrevel/revel-backend/issues/1085)).

These come on top of what tickets already showed: event name, venue and address, date and time,
ticket tier, and sector/seat.

Tickets issued before this change were backfilled: they were numbered per organization in creation
order, with the issue time set to their creation time.

Limitation: organizations without a VAT ID have no tax-ID line, because there is no separate
fiscal-code field yet.

## Attendee VAT handling

- Payments are Stripe Connect Standard **direct charges**: the organizer is the merchant of record.
- VAT on physical admission is charged at the rate of the country where the event takes place
  (place of supply).
- Attendee invoicing is opt-in per organization: `NONE` (default), `HYBRID` (drafts are reviewed,
  then sent) or `AUTO` (generated and sent automatically). Invoices are PDFs issued in the
  organizer's name.
- Turning invoicing on requires a VIES-validated EU VAT ID, or, for organizers under a national
  small-business scheme ([#1244](https://github.com/letsrevel/revel-backend/issues/1244); today
  Germany's § 19 UStG and Austria's § 6 Abs. 1 Z 27 UStG), the declared scheme plus a format-valid
  national tax registration number (EN 16931 BT-32) and establishment in the scheme's country. Scheme
  organizers charge no VAT: every invoice line is exempt (E) with the statutory note and reverse charge is
  never applied. A scheme covers domestic supplies only (the EU SME scheme's "-EX" registration is not
  supported), so a physical event held outside the scheme's country and a virtual event bought from
  outside it are not invoiced but recorded as skipped fiscal documents (`small_business_foreign_venue`,
  `small_business_virtual_cross_border`); an invoice whose seller cannot be identified at all is skipped
  as `seller_identity_missing`. VAT-charging organizers without an EU VAT ID remain unsupported: they
  need the (free) VAT ID. Details in [de.md](de.md) and [at.md](at.md).
- The country of establishment is the declared VAT country, else the VAT ID's prefix, else the
  structured billing address's country, else the organization's city. Every country policy keys on it.
- Revel's own platform-fee invoices are B2B invoices from an Austrian business (reverse charge within
  the EU for organizers with a validated VAT ID; Austrian VAT otherwise, including for small-business
  organizers, Art. 18 IR 282/2011; to confirm with the tax advisor) and are not affected by the country
  restrictions below.

See [Billing & VAT](../../architecture/billing-and-vat.md) for the details.

## Country status

| Country | Status | Layer 1 restriction | Issue |
|---|---|---|---|
| [Austria](at.md) | No country-specific restrictions identified; Kleinunternehmer (§ 6 Abs. 1 Z 27 UStG) without a UID may invoice through Revel with their Steuernummer | None; organizer notice on door payments (own Registrierkasse) | [#1244](https://github.com/letsrevel/revel-backend/issues/1244) |
| [Belgium](be.md) | Restricted | Invoices to buyers with a BE VAT ID blocked, for organizers established in Belgium (Peppol) | [#1066](https://github.com/letsrevel/revel-backend/issues/1066) |
| [Bulgaria](bg.md) | Not yet researched | None (default policy) | |
| [Croatia](hr.md) | Restricted | Attendee invoicing blocked for organizers established in Croatia (fiscalization); organizer notice to fiscalize invoices from their own software | [#1058](https://github.com/letsrevel/revel-backend/issues/1058) |
| [Cyprus](cy.md) | Not yet researched | None (default policy) | |
| [Czechia](cz.md) | No country-specific restrictions identified | None | |
| [Denmark](dk.md) | No country-specific restrictions identified | None; organizer notice on digital sales registration (e.g. discos) | |
| [Estonia](ee.md) | Not yet researched | None (default policy) | |
| [Finland](fi.md) | No country-specific restrictions identified | None | |
| [France](fr.md) | Restricted | Invoices to buyers with a FR VAT ID blocked, for organizers established in France (Plateforme Agréée); ticket content requirements covered | [#1061](https://github.com/letsrevel/revel-backend/issues/1061), [#1233](https://github.com/letsrevel/revel-backend/issues/1233) |
| [Germany](de.md) | No restriction; eligible attendee invoices and credit notes are issued as ZUGFeRD / Factur-X EN 16931 PDFs (domestic B2B invoices above 250 EUR must be E-Rechnungen once the transition ends: 2027-01-01 above 800k EUR prior-year turnover, 2028-01-01 otherwise; Kleinunternehmer exempt). Kleinunternehmer (§ 19 UStG) without a USt-IdNr. may invoice through Revel with their Steuernummer (BT-32); VAT-charging organizers without a USt-IdNr. cannot | None | [#1232](https://github.com/letsrevel/revel-backend/issues/1232), [#1235](https://github.com/letsrevel/revel-backend/issues/1235), [#1244](https://github.com/letsrevel/revel-backend/issues/1244) |
| [Greece](gr.md) | Restricted | Attendee invoicing blocked for organizers established in Greece and for physical events held there (myDATA); organizer notice to issue documents from their own software | [#1063](https://github.com/letsrevel/revel-backend/issues/1063) |
| [Hungary](hu.md) | Restricted | Attendee invoicing blocked for organizers established in Hungary and for physical events held there (NAV Online Számla); organizer notice on receipts, invoices and NAV reporting | [#1065](https://github.com/letsrevel/revel-backend/issues/1065) |
| [Ireland](ie.md) | No country-specific restrictions identified | None | |
| [Italy](it.md) | Restricted | Online payment blocked for events held in Italy (certified fiscal ticketing); offline payment allowed | [#1057](https://github.com/letsrevel/revel-backend/issues/1057) |
| [Latvia](lv.md) | Not yet researched | None (default policy) | |
| [Lithuania](lt.md) | Not yet researched | None (default policy) | |
| [Luxembourg](lu.md) | Not yet researched | None (default policy) | |
| [Malta](mt.md) | Not yet researched | None (default policy) | |
| [Netherlands](nl.md) | No country-specific restrictions identified | None | |
| [Poland](pl.md) | Restricted | Invoices to business buyers (any country) blocked, for organizers established in Poland (KSeF); organizer notice on the fiscal cash register (consumer admission to discos, dance halls, amusement and theme parks, circus) | [#1067](https://github.com/letsrevel/revel-backend/issues/1067) |
| [Portugal](pt.md) | Restricted | Attendee invoicing blocked for organizers established in Portugal (certified invoicing software) | [#1060](https://github.com/letsrevel/revel-backend/issues/1060) |
| [Romania](ro.md) | Restricted | Attendee invoicing blocked for organizers established in Romania (RO e-Factura) | [#1064](https://github.com/letsrevel/revel-backend/issues/1064) |
| [Slovakia](sk.md) | No country-specific restrictions identified | None | |
| [Slovenia](si.md) | Restricted | Attendee invoicing blocked for organizers established in Slovenia and for physical events held there (FURS invoice verification); organizer notice to issue verified invoices from their own software, invoicing off included | [#1062](https://github.com/letsrevel/revel-backend/issues/1062) |
| [Spain](es.md) | Upcoming (1 Jan 2027); Basque Country in force | Attendee invoicing blocked from 1 January 2027 for organizers established in Spain (Verifactu); allowed until then, with an organizer notice announcing the date. Organizers in the Basque Country are blocked now (TicketBAI, no notice); Navarre keeps the 2027 date with its own wording (NaTicket) | [#1059](https://github.com/letsrevel/revel-backend/issues/1059), [#1086](https://github.com/letsrevel/revel-backend/issues/1086) |
| [Sweden](se.md) | No country-specific restrictions identified | None | |

"No country-specific restrictions identified" means research found no rule that requires Revel to
block anything; it is not a guarantee that organizers there have no obligations of their own.
"Not yet researched" means exactly that: no claim is made about the country's rules.
