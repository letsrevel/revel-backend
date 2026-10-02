# Austria

Policy module: `src/events/compliance/policies/at.py` (`AustriaPolicy`: no restriction, one organizer notice).

## Status

**No country-specific restrictions identified for Revel's online sales.** Payments taken at the venue
are the organizer's own cash-register matter (see below).

Research found no rule that requires Revel to block attendee invoicing or online or offline payments
for organizers in Austria. This is not a guarantee that organizers have no obligations of their own: as
merchant of record they remain responsible for their tax and invoicing duties.

## What Revel does

- No country-specific restriction: attendee invoicing and online/offline payments are allowed.
- **Organizer notice (non-blocking)**, for organizers established in Austria and events held there,
  shown next to the offline / at-the-door payment setting: "Payments you take at the door go through
  your own registered cash register (Registrierkasse) once you pass the legal thresholds. Revel's
  online sales are exempt."
- The [common ticket content](index.md#common-ticket-content) applies.

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

## Open questions

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
