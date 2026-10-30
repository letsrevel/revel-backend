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

if t.TYPE_CHECKING:
    from events.models import Organization


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

# What the number is called on the invoice, per seller country.
_TAX_NUMBER_LABELS: t.Final[dict[str, str]] = {"DE": "Steuernummer", "AT": "Steuernummer"}
GENERIC_TAX_NUMBER_LABEL: t.Final = "Tax registration no."


def is_small_business(org: "Organization") -> bool:
    """Whether the organizer trades under a small-business VAT scheme (#1244): no VAT, no reverse charge."""
    return org.small_business_scheme in SCHEME_COUNTRIES


def scheme_country(scheme: str) -> str:
    """The country a scheme belongs to, or ``""`` for ``none`` / unknown."""
    return SCHEME_COUNTRIES.get(scheme, "")


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
    return digits.isdigit() and len(digits) in lengths


def tax_number_label(country: str) -> str:
    """How the seller's tax registration number is labelled on a document for ``country``."""
    return _TAX_NUMBER_LABELS.get(country.upper(), GENERIC_TAX_NUMBER_LABEL)
