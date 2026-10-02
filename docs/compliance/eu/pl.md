# Poland

Policy module: `src/events/compliance/policies/pl.py` (`PolandPolicy`, using `B2BEInvoicingMixin` with `b2b_buyer_scope = ANY_BUSINESS`, plus one organizer notice).

Issue: [#1067](https://github.com/letsrevel/revel-backend/issues/1067)

## Status

**Restricted: attendee invoices to business buyers blocked for organizers established in Poland.**

KSeF (Krajowy System e-Faktur) is mandatory for invoices issued by taxpayers established in Poland:
from 1 February 2026 for large taxpayers, 1 April 2026 for everyone else, and 1 January 2027 for
small taxpayers, when financial penalties also start. Invoices to consumers are excluded. A PDF
invoice from a Polish organizer to a business buyer, Polish or foreign, is not valid under KSeF: the
exclusions cover consumers and sellers without a Polish establishment, not foreign buyers.

## What Revel does

- **Attendee invoicing can be enabled.** HYBRID and AUTO are not refused for Polish organizations.
- **B2B invoices are skipped.** For organizers established in Poland, invoice generation, issuing an
  existing draft (including drafts created before the gate) and credit notes are skipped whenever
  the buyer is a business: a VAT ID from **any country** that VIES confirmed as valid at checkout or
  could not check. A VAT ID that VIES rejected (a typo, a made-up number) makes the buyer a
  consumer, who gets Revel's invoice as usual. The organizer must issue business invoices in KSeF
  from its own system.
  [KSeF: zakres obowiązkowego KSeF](https://ksef.podatki.gov.pl/informacje-ogolne-ksef-20/zakres-obowiazkowego-ksef/)
- **Consumers** still receive Revel's PDF invoice; KSeF excludes invoices to consumers.
- **Foreign organizers' events held in Poland are not affected**: KSeF covers taxpayers with a seat or
  fixed establishment in Poland.
- Online and offline payments are not restricted.
- **Organizer notice (non-blocking)**, for organizers established in Poland and events held there,
  shown next to the ticket-sales settings (topic `ticket_sales`, since the rule covers online sales
  too): "Admission sold to consumers for discos, dance halls, amusement and theme parks, and circus
  performances must be recorded on your own fiscal cash register (kasa fiskalna), even when paid online. For other events, the
  online-payment exemption applies only if your records link each payment to its sale." The duty
  covers sales to consumers only (VAT Act art. 111(1)); the payment-to-sale link is the condition of
  the bank-payment exemption (annex poz. 42), not a separate duty. Revel cannot act as a Polish fiscal
  cash register and cannot tell a disco from a concert, so the notice goes to every sale Poland
  reaches; the duty is the organizer's.
- The [common ticket content](index.md#common-ticket-content) applies.

## Legal basis

- **KSeF mandate and dates**:
  [KSeF rules](https://ksef.podatki.gov.pl/ksef-news/zasady-obowiazywania-ksef-i-przepisy-prawne/)
- **VAT Act art. 106ga ust. 2 pkt 4**: no obligation to issue structured invoices to consumers.
  [KSeF Q&A](https://ksef.podatki.gov.pl/pytania-i-odpowiedzi-ksef-20/)
- **VAT Act art. 111(1)**: the cash-register duty covers sales to individuals not conducting business
  (and lump-sum farmers).
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
- A per-transaction payment evidence export linking each payment and Stripe charge to ticket, event,
  amount, VAT and payout.

See [#1067](https://github.com/letsrevel/revel-backend/issues/1067).
