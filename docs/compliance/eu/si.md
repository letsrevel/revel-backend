# Slovenia

Policy module: `src/events/compliance/policies/si.py` (`SloveniaPolicy`, using `FiscalizedInvoicingMixin`).

Issue: [#1062](https://github.com/letsrevel/revel-backend/issues/1062)

## Status

**Restricted: attendee invoicing blocked.**

The Fiscal Verification of Invoices Act (ZDavPR) requires every invoice paid "in cash" to be verified
with FURS in real time. For ZDavPR, "cash" includes card payments, and current FURS guidance names
Stripe explicitly: payments received in batches through a platform count as cash. Revel's attendee
invoices are not verified with FURS.

## What Revel does

- **Attendee invoicing cannot be enabled.** Switching to HYBRID or AUTO is refused (HTTP 422) for
  organizations whose resolved country is Slovenia, with a translated explanation that invoices must
  go through FURS invoice verification.
- **Generation is skipped** (invoice generation, issuing an existing draft, credit notes) whenever
  Slovenia is a liable country of the sale: the organization is established there, or the event is a
  physical event in Slovenia.
- **Existing settings:** organizations in Slovenia that had HYBRID or AUTO were switched to NONE by
  the data migration `0128_disable_blocked_attendee_invoicing`. Existing invoices were not touched. No
  notification was sent automatically, so affected organizers should be informed out of band.
- Paid ticketing is not restricted.
- The [common ticket content](index.md#common-ticket-content) applies.

## Legal basis

- **ZDavPR**: card and other non-direct payments count as cash (Art. 2(4)); who is liable and the
  exemptions, none of which covers event admission (Art. 3); procedure, own certificate, premises
  registration (Art. 4–8); invoice content and three-part numbering (Art. 5); changes and refunds must
  also be verified (Art. 6(4)); penalties, including for the software supplier (Art. 18–19).
  [PISRS consolidated](https://pisrs.si/pregledPredpisa?id=ZAKO7195),
  [Uradni list 57/15](https://www.uradni-list.si/glasilo-uradni-list-rs/vsebina/2015-01-2372),
  amendments [ZDavPR-A](https://www.uradni-list.si/glasilo-uradni-list-rs/vsebina/2017-01-3270),
  [ZDavPR-B](https://www.uradni-list.si/glasilo-uradni-list-rs/vsebina/2023-01-1127),
  [ZDavPR-C](https://www.uradni-list.si/glasilo-uradni-list-rs/vsebina/2024-01-3104)
- **FURS detailed description** (4th ed., August 2026), §3.2.4 and §3.3 on online payment platforms
  including Stripe, §9.14.5 on tickets.
  [FURS](https://www.fu.gov.si/fileadmin/Internet/Nadzor/Podrocja/Davcne_blagajne_in_VKR/Opis/Davcne_blagajne_in_davcno_potrjevanje_racunov.docx)
- **Regulation implementing ZDavPR** (ZOI and QR code):
  [FURS](https://www.fu.gov.si/fileadmin/Internet/Nadzor/Podrocja/Davcne_blagajne_in_VKR/Zakonodaja/EN_Pravilnik_o_izvajanju_Zakona_o_davcnem_potrjevanju_racunov_s_prilogami.docx),
  [technical specification v3.1](https://www.datoteke.fu.gov.si/dpr/files/TehnicnaDokumentacijaVer3.1.pdf)
- Persons liable and the association exemption (ZDDV-1 Art. 81.a(6), taxable supplies up to €5,000 a
  year for qualifying non-profit associations):
  [FURS, persons liable](https://www.fu.gov.si/fileadmin/Internet/Nadzor/Podrocja/Davcne_blagajne_in_VKR/Opis/EN_Zavezanec_za_izvajanje_postopka_davcnega_potrjevanja_racunov.doc),
  [FURS, Računi](https://www.fu.gov.si/fileadmin/Internet/Davki_in_druge_dajatve/Podrocja/Davek_na_dodano_vrednost/Opis/Racuni.doc),
  [eUprava, associations](https://e-uprava.gov.si/si/podrocja/drzava-druzba/drustva-javne-prireditve/obveznosti-drustva-do-financne-uprave.html)
- [SPOT, online retail](https://spot.gov.si/sl/dejavnosti-in-poklici/dejavnosti/trgovina-na-drobno-po-posti-ali-po-internetu),
  [FURS overview (EN)](https://www.fu.gov.si/en/supervision/areas_of_work/fiscal_verification_of_invoices_and_pre_numbered_receipt_book),
  [FURS Q&A 2015 (partly superseded)](https://www.fu.gov.si/fileadmin/Internet/Nadzor/Podrocja/Davcne_blagajne_in_VKR/150717_Davcne_blagajne_-_Vprasanja_in_odgovori.pdf)

## Open questions

- Whether Revel could verify with its own certificate, or must use the organizer's.
- Whether verification must happen at payment or at the latest when the event takes place.
- Whether foreign organizers holding events in Slovenia "keep business books" in the ZDavP-2 sense.
- The 2015 FURS Q&A said PSP card payments need no verification; the 2026 guidance narrows this.
  Not confirmed by FURS for Stripe specifically.
- Free tickets and zero-amount orders are presumably out of scope; not explicitly confirmed.

## Later layers

- Organizer guidance for Slovenia (FURS certificate, internal act, premises type, the association
  exemption) and possibly a self-declared exemption flag.
- Optional native FURS verification: per-organization certificate and premises configuration,
  Slovenian numbering, a verification client computing ZOI and storing EOR, verified credit notes,
  and fiscal data with QR code on the PDF.

See [#1062](https://github.com/letsrevel/revel-backend/issues/1062).
