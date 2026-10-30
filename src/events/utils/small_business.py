"""Small-business VAT schemes (#1244): which country each applies in, its invoice wording, tax-number formats.

Pure utility: no DB access, no imports from services or models (the model imports the enum from here,
``events/models/organization.py`` being at the file-length cap).

Legal basis:

- **DE, § 19 UStG** (*Kleinunternehmer*): supplies are ``steuerfrei`` (since 1 January 2025). The
  invoice content is reduced by **§ 34a UStDV** to: names and addresses, the seller's *Steuernummer*
  or USt-IdNr. (or Kleinunternehmer-Identifikationsnummer), the issue date, the supply, the
  consideration as one amount and a note that the small-business exemption applies. § 34a UStDV
  also lets such an invoice always be a *sonstige Rechnung* (no E-Rechnung duty).
- **AT, § 6 Abs. 1 Z 27 UStG 1994** (*Kleinunternehmer*): exempt turnover (*unecht befreit*). The
  invoice content of **§ 11 Abs. 1 Z 3 UStG** applies; lit. e requires a note that an exemption
  applies and lit. i requires the seller's UID only for supplies with a right to input-tax
  deduction, which exempt small-business supplies have not. § 11 Abs. 6 lets a Kleinunternehmer
  use the small-amount invoice content.
- **EN 16931**: every line is category E (exempt) with a reason text (BR-E-10); the seller is
  identified by its tax registration identifier (BT-32), which BR-E-02 accepts in place of the
  VAT identifier (BT-31). Reverse charge (AE) needs a seller VAT identifier (BR-AE-02/03), so a
  scheme organizer never produces it.
"""

import re
import typing as t

from django.db import models

from common.country import normalize_country_code

if t.TYPE_CHECKING:
    from events.models import Organization

# Stable codes of the refusals below (``Decision.code``); ``SkippedFiscalDocument.ReasonCode`` mirrors them.
# A scheme covers domestic supplies only (absent the EU SME scheme's "-EX" registration, which Revel does
# not support): admission to a physical event is taxed where it takes place (Art. 53/54 of Directive
# 2006/112/EC) and virtual attendance where the customer is (Art. 44, Art. 54(1) as amended by Directive
# (EU) 2022/542), so a sale whose place of supply is outside the scheme's country is not invoiced.
SMALL_BUSINESS_FOREIGN_VENUE: t.Final = "small_business_foreign_venue"
SMALL_BUSINESS_VIRTUAL_CROSS_BORDER: t.Final = "small_business_virtual_cross_border"
# The seller cannot be identified on an invoice: no VIES-validated VAT ID and no complete scheme declaration.
SELLER_IDENTITY_MISSING: t.Final = "seller_identity_missing"


class SmallBusinessScheme(models.TextChoices):
    """National small-business VAT scheme an organizer trades under (``Organization.small_business_scheme``).

    A declared scheme means no VAT is charged on any sale (every invoice line is EN 16931 category E
    at 0% with the scheme's statutory note) and no reverse charge is ever applied.
    """

    NONE = "none", "None"
    DE_19 = "de_19", "Germany — Kleinunternehmer (§ 19 UStG)"
    AT_6_1_27 = "at_6_1_27", "Austria — Kleinunternehmer (§ 6 Abs. 1 Z 27 UStG)"


# Scheme → the ISO 3166-1 country whose law grants it (the organizer must be established there).
SCHEME_COUNTRIES: t.Final[dict[str, str]] = {SmallBusinessScheme.DE_19: "DE", SmallBusinessScheme.AT_6_1_27: "AT"}

# Prefilled exemption reason (BT-120, printed on the PDF): the statutory note each law requires.
# DE: § 34a S. 1 Nr. 5 UStDV ("... dass ... die Steuerbefreiung für Kleinunternehmer gilt").
# AT: § 11 Abs. 1 Z 3 lit. e UStG ("Hinweis, dass ... eine Steuerbefreiung gilt"); the wording is the
# one on the WKO's Kleinunternehmer invoice template.
SCHEME_EXEMPTION_REASONS: t.Final[dict[str, str]] = {
    SmallBusinessScheme.DE_19: "Steuerbefreit gemäß § 19 UStG (Kleinunternehmerregelung)",
    SmallBusinessScheme.AT_6_1_27: "Umsatzsteuerbefreit – Kleinunternehmer gem. § 6 Abs. 1 Z 27 UStG",
}

# Tax registration number (BT-32) formats, on the digits alone (separators ``/``, ``-``, ``.`` and
# spaces stripped). Only a plausibility check: these numbers cannot be looked up anywhere.
#   DE Steuernummer: 10 or 11 digits in the Land's format (e.g. 12/345/67890), or the 13-digit
#       bundeseinheitliches Format used by ELSTER (e.g. 1121081508150).
#   AT Steuernummer (Abgabenkontonummer): 2-digit Finanzamt + 7 digits (e.g. 12-345/6789).
_TAX_NUMBER_DIGITS: t.Final[dict[str, frozenset[int]]] = {"DE": frozenset({10, 11, 13}), "AT": frozenset({9})}
_SEPARATORS: t.Final = re.compile(r"[\s/.\-]")

# What the number is called on the invoice, per seller country. Plain strings on purpose: the invoice
# PDF templates are English-only ("INVOICE", "VAT:", "Bill To" are not translated either), and the
# document is a fiscal record, so its labels never follow the viewer's UI language. Only the national
# name is used where one is known; the generic label is unreachable while schemes exist for DE and AT only.
_TAX_NUMBER_LABELS: t.Final[dict[str, str]] = {"DE": "Steuernummer", "AT": "Steuernummer"}
GENERIC_TAX_NUMBER_LABEL: t.Final = "Tax registration no."


def is_small_business(org: "Organization") -> bool:
    """Whether the organizer trades under a small-business VAT scheme (#1244): no VAT, no reverse charge."""
    return org.small_business_scheme in SCHEME_COUNTRIES


def scheme_country(scheme: str) -> str:
    """The country a scheme belongs to, or ``""`` for ``none`` / unknown."""
    return SCHEME_COUNTRIES.get(scheme, "")


def scheme_sale_block(scheme: str, *, is_virtual: bool, venue_country: str, buyer_country: str) -> str:
    """Why a scheme organizer's sale is not invoiced by Revel: a refusal code, or ``""`` when it is.

    A physical event held outside the scheme's country is :data:`SMALL_BUSINESS_FOREIGN_VENUE`; a
    virtual event bought from outside it (EU business, EU consumer or non-EU alike: the place of
    supply moves to the customer under the 2025 rules) is :data:`SMALL_BUSINESS_VIRTUAL_CROSS_BORDER`.
    A blank venue or buyer country is read as domestic, the same assumption checkout prices on.
    """
    home = scheme_country(scheme)
    if not home:
        return ""
    if is_virtual:
        buyer = normalize_country_code(buyer_country)
        return SMALL_BUSINESS_VIRTUAL_CROSS_BORDER if buyer and buyer != home else ""
    venue = normalize_country_code(venue_country)
    return SMALL_BUSINESS_FOREIGN_VENUE if venue and venue != home else ""


def scheme_exemption_reason(scheme: str) -> str:
    """The statutory exemption note prefilled for a scheme, or ``""`` for ``none`` / unknown."""
    return SCHEME_EXEMPTION_REASONS.get(scheme, "")


def tax_number_format_known(country: str) -> bool:
    """Whether a format check exists for ``country``'s tax registration number."""
    return country.upper() in _TAX_NUMBER_DIGITS


def tax_number_is_valid(country: str, number: str) -> bool:
    """Format-check a tax registration number for ``country``.

    Returns:
        ``True`` when the number, separators stripped, is all digits of a length the country uses.
        ``False`` for a blank number. Countries without a known format accept any non-blank value.
    """
    digits = _SEPARATORS.sub("", number.strip())
    if not digits:
        return False
    lengths = _TAX_NUMBER_DIGITS.get(country.upper())
    if lengths is None:
        return True
    # ASCII digits only: ``str.isdigit`` also accepts other scripts' digits, which must not reach BT-32 or the PDF.
    return digits.isascii() and digits.isdigit() and len(digits) in lengths


def scheme_matches_country(scheme: str, country: str) -> bool:
    """Whether ``country`` (the resolved country of establishment) is the one ``scheme`` applies in."""
    return normalize_country_code(country) == scheme_country(scheme)


def scheme_identity_complete(scheme: str, vat_id_validated: bool, country: str, tax_number: str) -> bool:
    """The one rule for "this seller can be identified on an invoice" as far as #1244 is concerned.

    An organizer without a scheme is outside this feature (today's behaviour). A declared scheme must be
    the one of the country of establishment, validated VAT ID or not. Then a VIES-validated VAT ID (BT-31)
    identifies the seller; without one, the tax registration number (BT-32) must be format-valid for
    that country.
    """
    if not scheme_country(scheme):
        return True
    if not scheme_matches_country(scheme, country):
        return False
    return vat_id_validated or tax_number_is_valid(scheme_country(scheme), tax_number)


def tax_number_label(country: str) -> str:
    """How the seller's tax registration number is labelled on a document for ``country``."""
    return _TAX_NUMBER_LABELS.get(country.upper(), GENERIC_TAX_NUMBER_LABEL)
