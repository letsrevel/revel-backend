"""Country-code helpers shared across apps."""

import typing as t

# VAT/VIES prefixes that differ from the ISO 3166-1 code.
_VAT_PREFIX_TO_ISO: t.Final[dict[str, str]] = {"EL": "GR"}


def normalize_country_code(code: str | None) -> str:
    """Upper-case a country code and map VAT-only prefixes (``EL``) to ISO 3166-1."""
    upper = (code or "").strip().upper()
    return _VAT_PREFIX_TO_ISO.get(upper, upper)
