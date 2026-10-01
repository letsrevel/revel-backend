"""Country → policy registry, and resolution of an organization's country."""

import typing as t

from django.core.exceptions import ImproperlyConfigured

from events.compliance.base import CountryCompliancePolicy, DefaultEUPolicy

if t.TYPE_CHECKING:
    from events.models import Organization

_P = t.TypeVar("_P", bound=type[CountryCompliancePolicy])

_REGISTRY: dict[str, type[CountryCompliancePolicy]] = {}

# VAT/VIES prefixes that differ from the ISO 3166-1 code.
_VAT_PREFIX_TO_ISO: t.Final[dict[str, str]] = {"EL": "GR"}


def normalize_country_code(code: str | None) -> str:
    """Upper-case a country code and map VAT-only prefixes (``EL``) to ISO 3166-1."""
    upper = (code or "").strip().upper()
    return _VAT_PREFIX_TO_ISO.get(upper, upper)


def register(country: str) -> t.Callable[[_P], _P]:
    """Class decorator registering a policy for an ISO 3166-1 alpha-2 country.

    Raises:
        ImproperlyConfigured: On a malformed code or a second policy for the same country.
    """
    code = normalize_country_code(country)
    if len(code) != 2 or not code.isalpha():
        raise ImproperlyConfigured(f"Compliance policy country must be ISO 3166-1 alpha-2, got {country!r}.")

    def decorator(cls: _P) -> _P:
        """Record ``cls`` as the policy for ``code``."""
        existing = _REGISTRY.get(code)
        if existing is not None and existing is not cls:
            raise ImproperlyConfigured(f"Two compliance policies for {code}: {existing!r} and {cls!r}.")
        _REGISTRY[code] = cls
        return cls

    return decorator


def registered_policies() -> dict[str, type[CountryCompliancePolicy]]:
    """A copy of the registry (country → policy class)."""
    return dict(_REGISTRY)


def get_policy_for_country(country: str | None) -> CountryCompliancePolicy:
    """The policy for a country code (VAT prefixes accepted); the EU default when none is registered."""
    code = normalize_country_code(country)
    return _REGISTRY.get(code, DefaultEUPolicy)(code)


def resolve_country(vat_country_code: str | None, vat_id: str | None, city_iso2: str | None) -> str:
    """Resolve an organizer's country of establishment from what it has declared.

    Order: the declared VAT country (set by the organizer, by VIES validation or from
    the Stripe Connect account) → the VAT ID prefix → the organization's city.

    Returns:
        An ISO 3166-1 alpha-2 code, or ``""`` when nothing is declared.
    """
    if country := normalize_country_code(vat_country_code):
        return country
    prefix = (vat_id or "").strip()[:2]
    if len(prefix) == 2 and prefix.isalpha():
        return normalize_country_code(prefix)
    return normalize_country_code(city_iso2)


def resolve_org_country(org: "Organization") -> str:
    """Resolve an organization's country (see :func:`resolve_country`); select ``city`` on hot paths."""
    city_iso2 = org.city.iso2 if org.city_id and org.city else ""
    return resolve_country(org.vat_country_code, org.vat_id, city_iso2)


def get_policy(org: "Organization") -> CountryCompliancePolicy:
    """The policy for an organization's resolved country."""
    return get_policy_for_country(resolve_org_country(org))
