"""Shared constants used across multiple apps."""

# Basic format: 2-letter country prefix + 2-13 alphanumeric characters
VAT_ID_PATTERN = r"^[A-Z]{2}[0-9A-Z]{2,13}$"

EU_MEMBER_STATES: frozenset[str] = frozenset(
    {
        "AT",  # Austria
        "BE",  # Belgium
        "BG",  # Bulgaria
        "CY",  # Cyprus
        "CZ",  # Czech Republic
        "DE",  # Germany
        "DK",  # Denmark
        "EE",  # Estonia
        "ES",  # Spain
        "FI",  # Finland
        "FR",  # France
        "EL",  # Greece (VIES/VAT prefix)
        "GR",  # Greece (ISO 3166-1)
        "HR",  # Croatia
        "HU",  # Hungary
        "IE",  # Ireland
        "IT",  # Italy
        "LT",  # Lithuania
        "LU",  # Luxembourg
        "LV",  # Latvia
        "MT",  # Malta
        "NL",  # Netherlands
        "PL",  # Poland
        "PT",  # Portugal
        "RO",  # Romania
        "SE",  # Sweden
        "SI",  # Slovenia
        "SK",  # Slovakia
    }
)
