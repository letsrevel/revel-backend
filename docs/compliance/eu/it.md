# Italy

Policy module: `src/events/compliance/policies/it.py` (`ItalyPolicy`, using `CertifiedOnlineTicketingMixin`).

Issue: [#1057](https://github.com/letsrevel/revel-backend/issues/1057)

## Status

**Restricted: online payment blocked for events in Italy.**

Paid *intrattenimenti* (club nights, DJ sets) and *spettacoli* (concerts, theatre) must be certified
with fiscal *titoli di accesso* issued by a system that the Agenzia delle Entrate (AdE) has recognised
as suitable and that is activated with a SIAE card. Online sales must go exclusively through an
AdE-approved online system. Revel is not an approved system.

## What Revel does

Only **online payment** is blocked, and only for **events held in Italy** (physical events whose VAT
country is Italy), whoever the organizer is:

- **Scope.** An Italian organizer's events held abroad are not affected, and neither are virtual
  events.
- **Offline payment still works.** Paid tiers with offline, bank-transfer or at-the-door payment
  methods (including pay-what-you-can with offline methods) are allowed and work as before, including
  the organizer's payment-confirmation dashboard.
- **No online payment method.** Creating a tier with the online payment method, or switching a tier to
  it, is refused (HTTP 422).
- **Existing online tiers.** Tiers that already used online payment before the restriction stay
  editable (rename, pause, switch to offline or at-the-door), but online checkout refuses them, as it
  refuses paid series passes sold online.
- **Unaffected:** free tickets, RSVPs, memberships and attendee invoicing.
- **Ticket notice.** Priced tickets for events held in Italy carry an extra line on the PDF and on
  Apple/Google Wallet passes: "Reservation only: this isn't a fiscal access ticket (titolo d'accesso).
  The organizer issues it."
- The [common ticket content](index.md#common-ticket-content) applies.

The refusal message, naming Italy in the user's language, says that online card payments aren't
available for events in Italy, because Italian law requires paid tickets sold online to be issued by a
ticketing system approved by the Agenzia delle Entrate, and that payment at the door or by bank
transfer still works, with payments confirmed from the dashboard. The organizer issues the fiscal
ticket itself.

## Legal basis

- **Provvedimento AdE 22 ottobre 2002**: activation cards, system suitability (*riconoscimento di
  idoneità*, valid 5 years, every change needs prior AdE authorization), certification by an
  accredited body. The card holder (*titolare*) must run the issuing system on Italian territory and
  can issue tickets for third-party organizers.
  [Provvedimento](https://www.agenziaentrate.gov.it/portale/documents/20143/275244/Provvedimento+del+22+10+2002_Provvedimento+AE+22+ottobre+2002.pdf/6cc8cbd3-5243-c35f-52a8-4d90222bf9ab)
- **Certification scope**: card-only issuance, fiscal seal (*sigillo fiscale*), single print,
  unalterable log, signed daily and monthly summaries, retention of ticket data for at least 60 days
  after the event and summaries for 2 years.
  [AdE, certificazione dei sistemi](https://www.agenziaentrate.gov.it/portale/documents/20143/274973/Certificazione+dei+sistemi+per+emissione+dei+titoli+di+accesso_certif_sistemi.pdf/439cfdd9-33b9-e55d-38ad-dee8afbd6da2)
- **Provvedimento AdE 27 giugno 2019 n. 223774** (as amended on 25 September 2025): online sales only
  through AdE-approved online systems; CAPTCHA; for events selling more than 1,000 tickets online,
  identified buyers, at most 10 tickets per user per event, and deferred ticket delivery.
  [Provv. 27/06/2019](https://www.agenziaentrate.gov.it/portale/documents/20143/1646898/Provvedimento%2Bdel%2B27%2Bgiugno%2B2019%2Bsecondary%2Bticketing.pdf/d72cb570-41b3-3c47-1bce-6aa4e98ef205),
  [Provv. 25/09/2025](https://www.agenziaentrate.gov.it/portale/documents/20143/275244/Provvedimento+del+25+settembre+2025+-+pdf.pdf/ef4c21fc-5f22-349b-3918-4a2025b68524)
- **Approved systems**: AdE keeps a list of approvals per version, including platform-specific variants
  of approved systems.
  [Elenco provvedimenti di idoneità](https://www.agenziaentrate.gov.it/portale/documents/20143/275176/ELENCO+PROVVEDIMENTI+DI+IDONEITA+2026+05+08.pdf/04f7156e-953b-01f4-3aec-856d26a544c1)
- Overview and forms: [AdE, Biglietterie automatizzate](https://www.agenziaentrate.gov.it/portale/schede/istanze/scheda-biglietterie-automatizzate/infogen-biglietterie-automatizzate),
  [Normativa e prassi (DM 13/07/2000, Provv. 23/07/2001)](https://www.agenziaentrate.gov.it/portale/schede/istanze/scheda-biglietterie-automatizzate/np-biglietterie-automatizzate)

The organizer owes the tax and must certify its takings; the *titolare* of the issuing system must run
it correctly and transmit to SIAE. Stripe is only the payment processor.

## Open questions

- Commercial terms and integration interface of a certified-system partner (not public).
- Who acts as *titolare* under existing platform variants of approved systems.
- Exact exemptions for associations, occasional organizers and small taxpayers. Organizers should
  confirm with their *commercialista*.
- Penalties were not researched.

## Later layers

- **Medium term:** integrate with the holder of an approved system as a provider under
  `src/integrations/providers/` (a "Revel" variant of an approved system, certified by an accredited
  body). Revel would register venues, organizers and events, request fiscal issuance per ticket at
  payment confirmation, store and print the fiscal data, handle cancellations as fiscal annulments, and
  meet the online-sales rules. This is the upgrade path that would lift the online-payment block.
- **Not recommended:** Revel becoming its own approved producer and *titolare*.

See [#1057](https://github.com/letsrevel/revel-backend/issues/1057).
