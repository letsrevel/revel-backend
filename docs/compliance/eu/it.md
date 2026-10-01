# Italy

Policy module: `src/events/compliance/policies/it.py` (`ItalyPolicy`, using `CertifiedTicketingMixin`).

Issue: [#1057](https://github.com/letsrevel/revel-backend/issues/1057)

## Status

**Restricted: paid ticketing blocked.**

Paid *intrattenimenti* (club nights, DJ sets) and *spettacoli* (concerts, theatre) must be certified
with fiscal *titoli di accesso* issued by a system that the Agenzia delle Entrate (AdE) has recognised
as suitable and that is activated with a SIAE card. Online sales must go exclusively through an
AdE-approved online system. Revel is not an approved system.

## What Revel does

For organizations whose resolved country is Italy, and for physical events held in Italy (by any
organizer):

- **No new paid tiers.** Creating a tier that can charge anything is refused: a price above zero,
  pay-what-you-can, or any seat-category price above zero. This applies to every payment method,
  including offline and at-the-door payments.
- **No turning a free tier paid.**
- **Checkout refuses paid carts.** Tiers that were already paid before the restriction stay editable,
  but checkout refuses any cart that costs anything, whatever the payment method. Paid series passes
  cannot be bought.
- **Unaffected:** free tiers, RSVPs and memberships.
- The block applies to the liable countries of the sale: the organization's resolved country and,
  for a physical event, the country where it takes place. Virtual events follow the organization only.
- Attendee invoicing is not restricted.
- The [common ticket content](index.md#common-ticket-content) applies.

The refusal message tells the organizer that tickets for paid events must be issued by an AdE-approved
fiscal ticketing system (SIAE).

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
  meet the online-sales rules. This is the upgrade path that would lift the paid-ticketing block.
- **Not recommended:** Revel becoming its own approved producer and *titolare*.

See [#1057](https://github.com/letsrevel/revel-backend/issues/1057).
