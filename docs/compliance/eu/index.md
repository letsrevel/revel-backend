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
- **Ticket number**: a gap-free sequential number per organization, in the form `SERIES-000123`.
    - The series is derived from the organization's slug when its first ticket is numbered, and never
      changes afterwards.
    - The number is assigned atomically when the ticket is first issued (it becomes ACTIVE or
      CHECKED_IN), never while it is pending.
    - Numbers are never reused, and cancelled tickets keep theirs.
- **Issued**: the issue date and time.
- **Price**: the price paid including VAT (for example `EUR 25.00`), or "Free" for zero-price tickets.
- **Tax notice**: "This ticket is not a tax invoice or receipt."

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
- Revel's own platform-fee invoices are B2B invoices from an Austrian business (reverse charge within
  the EU) and are not affected by the country restrictions below.

See [Billing & VAT](../../architecture/billing-and-vat.md) for the details.

## Country status

| Country | Status | Layer 1 restriction | Issue |
|---|---|---|---|
| [Austria](at.md) | No country-specific restrictions identified | None | |
| [Belgium](be.md) | Restricted | Invoices to buyers with a BE VAT ID blocked, for organizers established in Belgium (Peppol) | [#1066](https://github.com/letsrevel/revel-backend/issues/1066) |
| [Bulgaria](bg.md) | Not yet researched | None (default policy) | |
| [Croatia](hr.md) | Restricted | Attendee invoicing blocked for organizers established in Croatia (fiscalization) | [#1058](https://github.com/letsrevel/revel-backend/issues/1058) |
| [Cyprus](cy.md) | Not yet researched | None (default policy) | |
| [Czechia](cz.md) | No country-specific restrictions identified | None | |
| [Denmark](dk.md) | No country-specific restrictions identified | None | |
| [Estonia](ee.md) | Not yet researched | None (default policy) | |
| [Finland](fi.md) | No country-specific restrictions identified | None | |
| [France](fr.md) | No blocking restrictions (ticket content requirements covered) | None | [#1061](https://github.com/letsrevel/revel-backend/issues/1061) |
| [Germany](de.md) | No country-specific restrictions identified | None | |
| [Greece](gr.md) | Restricted | Attendee invoicing blocked for organizers established in Greece and for physical events held there (myDATA) | [#1063](https://github.com/letsrevel/revel-backend/issues/1063) |
| [Hungary](hu.md) | Restricted | Attendee invoicing blocked for organizers established in Hungary and for physical events held there (NAV Online Számla) | [#1065](https://github.com/letsrevel/revel-backend/issues/1065) |
| [Ireland](ie.md) | No country-specific restrictions identified | None | |
| [Italy](it.md) | Restricted | Online payment blocked for events held in Italy (certified fiscal ticketing); offline payment allowed | [#1057](https://github.com/letsrevel/revel-backend/issues/1057) |
| [Latvia](lv.md) | Not yet researched | None (default policy) | |
| [Lithuania](lt.md) | Not yet researched | None (default policy) | |
| [Luxembourg](lu.md) | Not yet researched | None (default policy) | |
| [Malta](mt.md) | Not yet researched | None (default policy) | |
| [Netherlands](nl.md) | No country-specific restrictions identified | None | |
| [Poland](pl.md) | Restricted | Invoices to buyers with a PL VAT ID blocked, for organizers established in Poland (KSeF) | [#1067](https://github.com/letsrevel/revel-backend/issues/1067) |
| [Portugal](pt.md) | Restricted | Attendee invoicing blocked for organizers established in Portugal (certified invoicing software) | [#1060](https://github.com/letsrevel/revel-backend/issues/1060) |
| [Romania](ro.md) | Restricted | Attendee invoicing blocked for organizers established in Romania (RO e-Factura) | [#1064](https://github.com/letsrevel/revel-backend/issues/1064) |
| [Slovakia](sk.md) | No country-specific restrictions identified | None | |
| [Slovenia](si.md) | Restricted | Attendee invoicing blocked for organizers established in Slovenia and for physical events held there (FURS invoice verification) | [#1062](https://github.com/letsrevel/revel-backend/issues/1062) |
| [Spain](es.md) | Upcoming (1 Jan 2027) | Attendee invoicing blocked from 1 January 2027 for organizers established in Spain (Verifactu); allowed until then | [#1059](https://github.com/letsrevel/revel-backend/issues/1059) |
| [Sweden](se.md) | No country-specific restrictions identified | None | |

"No country-specific restrictions identified" means research found no rule that requires Revel to
block anything; it is not a guarantee that organizers there have no obligations of their own.
"Not yet researched" means exactly that: no claim is made about the country's rules.
