# France

Policy module: `src/events/compliance/policies/fr.py` (`FrancePolicy`, a plain `DefaultEUPolicy`).

Issue: [#1061](https://github.com/letsrevel/revel-backend/issues/1061)

## Status

**No blocking restrictions (ticket content requirements covered).**

Organizers of shows with a paid entry whose receipts are subject to VAT must issue a ticket, or record
the entry data in a computerized system, before access (CGI art. 290 quater). The ticket must carry
specific mentions. Layer 1 covers those mentions through the common ticket content; the record-keeping,
reporting and declaration duties are left to later layers.

## What Revel does

- Attendee invoicing and paid ticketing are allowed.
- The [common ticket content](index.md#common-ticket-content) covers the art. 290 quater ticket
  mentions: identification of the organizer, the show, the seat category (tier), the **total price paid
  or the mention of free admission**, and a **system-assigned sequential number**.
- `FrancePolicy` exists, with no restriction, so later layers have a place to hook in.

Not done in layer 1: the operation journal and record retention (ticket rows are still deleted with
the event, tier or user account), the per-séance revenue statement, the SIBIL export and the
declaration pack.

## Legal basis

- **CGI art. 290 quater**: duty to issue a ticket or record the entry before access.
  [Légifrance](https://www.legifrance.gouv.fr/codes/article_lc/LEGIARTI000018619290)
- **BOFiP BOI-TVA-DECLA-20-30-20-30**: scope limited to shows whose receipts are subject to VAT (§70);
  e-tickets allowed; retention of electronic data for at least 3 years (§310), 6 years in general;
  declaration of the computerized system by the organizer (§270).
  [BOFiP](https://bofip.impots.gouv.fr/bofip/1054-PGP.html/identifiant=BOI-TVA-DECLA-20-30-20-30-20120912)
- **Cahier des charges**, arrêté of 8 March 1993 as amended by the arrêté of 5 October 2007: mandatory
  ticket data and system requirements (every operation kept, no change without a trace, revenue
  statement per séance).
  [Arrêté 1993](https://www.legifrance.gouv.fr/loda/id/JORFTEXT000000711316),
  [Arrêté 2007](https://www.legifrance.gouv.fr/jorf/id/JORFTEXT000000793411)
- **CGI Annexe IV** art. 50 sexies B and art. 50 sexies F (delivery declaration by holders of
  ticketing software).
  [50 sexies B](https://www.legifrance.gouv.fr/codes/article_lc/LEGIARTI000026203838),
  [50 sexies F](https://www.legifrance.gouv.fr/codes/article_lc/LEGIARTI000006301328)
- **SIBIL** (Loi 2016-925 art. 48, Décret 2017-926): licensed live-performance organizers send ticketing
  data every quarter, even when ticketing is outsourced.
  [Décret 2017-926](https://www.legifrance.gouv.fr/jorf/id/JORFTEXT000034640081),
  [Ministère de la Culture](https://www.culture.gouv.fr/aides-demarches/aides-demarches-et-subventions/dispositifs-specifiques/sibil-systeme-d-information-billetterie),
  [SIBIL FAQ](https://culture.gouv.fr/Thematiques/Theatre-spectacles/Pour-les-professionnels/SIBIL-Systeme-d-Information-BILletterie/FAQ-SIBIL-Questions-frequentes)
- **Tax on live shows** (CNM / ASTP), CIBS arts. L. 452-14 to L. 452-27: 3.5% of receipts excluding VAT,
  owed by the organizer.
  [Légifrance](https://www.legifrance.gouv.fr/codes/section_lc/LEGITEXT000044595989/LEGISCTA000048626026/),
  [CNM](https://cnm.fr/taxe/)
- **Secure cash-register software** (CGI art. 286 I 3° bis, "NF525"), with a tolerance when all
  payments go through certain payment intermediaries.
  [BOFiP BOI-TVA-DECLA-30-10-30](https://bofip.impots.gouv.fr/bofip/10691-PGP.html/identifiant=BOI-TVA-DECLA-30-10-30-20260325)

These obligations fall on the organizer (the *exploitant*), with the possible exception of art. 50
sexies F.

## Open questions

- Whether the art. 50 sexies F delivery declaration applies to a non-French SaaS platform that never
  delivers pre-numbered ticket stock. High impact, unverified.
- Whether a UUID would have been acceptable as the operation number (layer 1 uses a sequential number).
- Whether Stripe meets the NF525 tolerance for payments through credit institutions or EU banks.
- VAT-exempt associations are outside art. 290 quater but may still owe SIBIL and the show tax.
- The exact SIBIL data model; whether ticket mentions must be in French.
- French show VAT rates can already be set per tier; not re-verified.

## Later layers

- An inalterable operation journal and record retention (protecting ticket records from deletion).
- A revenue statement (*bordereau de recettes*) per séance, also serving the CNM/ASTP declaration.
- A quarterly SIBIL export, and possibly automated transmission.
- A versioned declaration pack for organizers (art. 50 sexies I), with notice when a release changes
  the declared elements.
- A documented NF525 position.

See [#1061](https://github.com/letsrevel/revel-backend/issues/1061).
