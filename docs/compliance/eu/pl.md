# Poland

Policy module: `src/events/compliance/policies/pl.py` (`PolandPolicy`, using `DomesticB2BEInvoicingMixin`).

Issue: [#1067](https://github.com/letsrevel/revel-backend/issues/1067)

## Status

**Restricted: domestic B2B attendee invoices blocked for organizers established in Poland.**

KSeF (Krajowy System e-Faktur) is mandatory for invoices issued by taxpayers established in Poland:
from 1 February 2026 for large taxpayers, 1 April 2026 for everyone else, and 1 January 2027 for
small taxpayers, when financial penalties also start. Invoices to consumers are excluded. A PDF
invoice from a Polish organizer to a Polish business buyer is not valid under KSeF.

## What Revel does

- **Attendee invoicing can be enabled.** HYBRID and AUTO are not refused for Polish organizations.
- **Domestic B2B invoices are skipped.** For organizers established in Poland, invoice generation,
  issuing an existing draft (including drafts created before the gate) and credit notes are skipped
  only when the **buyer's VAT ID starts with `PL`** and VIES confirmed it as valid at checkout or
  could not be reached. A VAT ID that VIES rejected (a typo, a made-up number) makes the buyer a
  consumer, who gets Revel's invoice as usual. The organizer must issue that invoice in KSeF.
- **Consumers** still receive Revel's PDF invoice; KSeF excludes invoices to consumers.
- **Known gap: foreign business buyers.** Revel still issues its PDF invoice to a business buyer
  with a non-Polish VAT ID. That is Revel's current behaviour, not a KSeF exemption: KSeF's
  exclusions cover consumers and sellers without a Polish establishment, not foreign buyers, so a
  Polish organizer may still have to issue that invoice in KSeF. Affected organizers should issue
  every invoice KSeF requires from their own system.
  [KSeF: zakres obowiązkowego KSeF](https://ksef.podatki.gov.pl/informacje-ogolne-ksef-20/zakres-obowiazkowego-ksef/)
- **Foreign organizers' events held in Poland are not affected**: KSeF covers taxpayers with a seat or
  fixed establishment in Poland.
- Online and offline payments are not restricted.
- The [common ticket content](index.md#common-ticket-content) applies.

Revel cannot act as a Polish fiscal cash register. Organizers selling admission to discos, dance halls
or circus performances must register those B2C sales on a cash register regardless of payment method;
this is the organizer's obligation.

## Legal basis

- **KSeF mandate and dates**:
  [KSeF rules](https://ksef.podatki.gov.pl/ksef-news/zasady-obowiazywania-ksef-i-przepisy-prawne/)
- **VAT Act art. 106ga ust. 2 pkt 4**: no obligation to issue structured invoices to consumers.
  [KSeF Q&A](https://ksef.podatki.gov.pl/pytania-i-odpowiedzi-ksef-20/)
- **Cash-register exemptions**, Rozporządzenie MF of 17 December 2024 (Dz.U. 2024 poz. 1902, amended by
  Dz.U. 2026 poz. 420): bank-mediated B2C payments are exempt if each payment is identifiable (annex
  poz. 42); turnover exemption up to PLN 20,000 a year (§3 ust. 1 pkt 1); no exemption for admission to
  circuses, amusement parks, discos and dance halls (§4 ust. 1 pkt 2 lit. k and l).
  [Dz.U. 2024 poz. 1902](https://api.sejm.gov.pl/eli/acts/DU/2024/1902/text.pdf),
  [Dz.U. 2026 poz. 420](https://api.sejm.gov.pl/eli/acts/DU/2026/420/text.pdf)
- **Interpretation 0114-KDIP1-3.4012.434.2024.2.MKW**: card payments through payment intermediaries
  count as bank-mediated.
  [Eureka](https://eureka.mf.gov.pl/informacje/podglad/603331)

## Open questions

- Interpretation 0114-KDIP1-3.4012.175.2025.2.KP could not be retrieved.
- Whether a party in a rented venue counts as admission to a disco or dance hall depends on the PKWiU
  classification; organizers should get their own ruling.
- Whether funds held on a Stripe balance before payout satisfy the poz. 42 bank-account condition.
- Whether gating B2B invoices during the 2026 no-penalty window was urgent.

## Later layers

- Optional KSeF API integration: FA(3) XML, per-organizer authentication, KSeF number and QR code,
  credit notes as corrective invoices.
- Cash-register guidance for Polish organizers.
- A per-transaction payment evidence export linking each payment and Stripe charge to ticket, event,
  amount, VAT and payout.

See [#1067](https://github.com/letsrevel/revel-backend/issues/1067).
