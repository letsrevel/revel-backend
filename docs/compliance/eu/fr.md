# France

Policy module: `src/events/compliance/policies/fr.py` (`FrancePolicy`, using `B2BEInvoicingMixin` with the default `b2b_buyer_scope = DOMESTIC`).

Issues: [#1061](https://github.com/letsrevel/revel-backend/issues/1061) (ticket rules),
[#1233](https://github.com/letsrevel/revel-backend/issues/1233) (e-invoicing)

## Status

**Restricted: domestic B2B attendee invoices blocked for organizers established in France; ticket
content requirements covered.**

Since 1 September 2026, domestic B2B invoices between taxable persons established in France must be
e-invoices issued through a *Plateforme Agréée* (PA): large companies and mid-sized companies (ETI)
must issue them now, SMEs and micro-enterprises from 1 September 2027, and every company must be able
to receive them. A PDF is not a valid invoice for these transactions. B2C sales are e-reported by the
seller, not e-invoiced.

Organizers of shows with a paid entry whose receipts are subject to VAT must issue a ticket, or record
the entry data in a computerized system, before access (CGI art. 290 quater). The ticket must carry
specific mentions. Layer 1 covers those mentions through the common ticket content; the record-keeping,
reporting and declaration duties are left to later layers.

## What Revel does

- **Attendee invoicing can be enabled.** HYBRID and AUTO are not refused for French organizations.
- **Domestic B2B invoices are skipped.** For organizers established in France, invoice generation,
  issuing an existing draft (including drafts created before the gate) and credit notes are skipped
  only when the **buyer's VAT ID starts with `FR`** and VIES confirmed it as valid at checkout or
  could not be reached. A VAT ID that VIES rejected makes the buyer a consumer, who gets Revel's
  invoice as usual. The organizer must issue that invoice through its own Plateforme Agréée.
- **The gate binds every French organizer now**, SMEs included, although SMEs only have to issue
  e-invoices from 1 September 2027. Revel cannot know an organizer's size, and French B2B ticket
  invoices are expected to be rare, so the conservative reading was chosen over a per-organizer
  "large company / ETI" flag ([#1233](https://github.com/letsrevel/revel-backend/issues/1233)).
- **Consumers and cross-border business buyers** still receive Revel's PDF invoice. B2C e-reporting
  is the organizer's (through its PA); Revel does not produce an e-reporting export.
- **Skipped documents are recorded** ([#1091](https://github.com/letsrevel/revel-backend/issues/1091)).
  The owner sees them under *Documents to issue yourself*
  (`GET /organization-admin/{slug}/skipped-fiscal-documents`) and marks each one done with the number
  from its own system (`POST …/{id}/resolve`). The ticket list flags those sales (`invoice_skipped`),
  and the revenue report has a *Documents to issue yourself* sheet.
- **Foreign organizers' events held in France are not affected**: a company without a French
  establishment is not subject to e-invoicing (only to e-reporting, where it applies).
- Online and offline payments are allowed.
- The [common ticket content](index.md#common-ticket-content) covers the art. 290 quater ticket
  mentions: identification of the organizer, the show, the seat category (tier), the **total price paid
  or the mention of free admission**, and a **system-assigned sequential number**.
- The revenue report's Transactions sheet carries each ticket's sequential number (`ticket_number`
  column), so organizers can reconcile it with their ticketing records (#1090).

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

- **E-invoicing reform** (CGI art. 289 bis and 290, décret 2026-677): domestic B2B e-invoicing through a
  Plateforme Agréée; issuing for large companies and ETI since 1 September 2026, for SMEs and
  micro-enterprises from 1 September 2027; receiving for all since 1 September 2026. Formats are
  Factur-X, UBL or CII under the EN 16931 France CIUS with the BR-FR rules.
  [DGFiP practical guide, July 2026](https://www.impots.gouv.fr/sites/default/files/media/1_metier/2_professionnel/EV/2_gestion/290_facturation_electronique/guide_pratique_facturation_electronique.pdf),
  [accredited platforms](https://www.impots.gouv.fr/facturation-electronique-et-plateformes-agreees),
  [external specifications](https://www.impots.gouv.fr/specifications-externes-b2b),
  [euinvoice country notes](https://letsrevel.github.io/euinvoice/countries/#france)
- **Foreign sellers without a French establishment**: e-reporting only, no e-invoicing.
  [impots.gouv.fr](https://www.impots.gouv.fr/internationalenbusiness/e-reporting-requirements-foreign-companies-without-permanent-establishment)
- **Billing mandate** (CGI art. 289 I-2): a third party may issue on the supplier's behalf, but the
  invoice still goes through a PA under the supplier's SIREN (secondary source).
  [guide-plateforme-agreee.fr](https://guide-plateforme-agreee.fr/mandat-de-facturation-facturation-electronique/)

These obligations fall on the organizer (the *exploitant*), with the possible exception of art. 50
sexies F.

## Open questions

- The Légifrance texts of the reform (CGI 289 bis and 290, décret 2026-677) were not retrieved; the
  dates come from the DGFiP guide. The guide mentions a start-up tolerance without sanctions but gives
  no end date; penalty amounts were not found.
- Associations (loi 1901) that are not taxable persons: presumably out of scope as organizers and as
  buyers, not confirmed.
- Whether organizers want an export their PA can use for B2C e-reporting.

- Whether the art. 50 sexies F delivery declaration applies to a non-French SaaS platform that never
  delivers pre-numbered ticket stock. High impact, unverified.
- Whether a UUID would have been acceptable as the operation number (layer 1 uses a sequential number).
- Whether Stripe meets the NF525 tolerance for payments through credit institutions or EU banks.
- VAT-exempt associations are outside art. 290 quater but may still owe SIBIL and the show tax.
- The exact SIBIL data model; whether ticket mentions must be in French.
- French show VAT rates can already be set per tier; not re-verified.

## Later layers

- Issuing domestic B2B invoices through a Plateforme Agréée under the organizer's billing mandate
  ([#1097](https://github.com/letsrevel/revel-backend/issues/1097)), building on EN 16931 output
  ([#1234](https://github.com/letsrevel/revel-backend/issues/1234),
  [#1235](https://github.com/letsrevel/revel-backend/issues/1235)) and euinvoice's French profile
  ([letsrevel/euinvoice#178](https://github.com/letsrevel/euinvoice/issues/178)).

- An inalterable operation journal and record retention (protecting ticket records from deletion).
- A revenue statement (*bordereau de recettes*) per séance, also serving the CNM/ASTP declaration.
- A quarterly SIBIL export, and possibly automated transmission.
- A versioned declaration pack for organizers (art. 50 sexies I), with notice when a release changes
  the declared elements.
- A documented NF525 position.

See [#1061](https://github.com/letsrevel/revel-backend/issues/1061) and
[#1233](https://github.com/letsrevel/revel-backend/issues/1233).
