"""Country → policy registry, and resolution of an organization's country (and subdivision)."""

import re
import typing as t

from django.core.exceptions import ImproperlyConfigured

from events.compliance.base import CountryCompliancePolicy, DefaultEUPolicy

if t.TYPE_CHECKING:
    from events.models import Organization

_P = t.TypeVar("_P", bound=type[CountryCompliancePolicy])

_REGISTRY: dict[str, type[CountryCompliancePolicy]] = {}
# (ISO 3166-1 country, case-folded ``City.admin_name``) -> registered ISO 3166-2 subdivision.
_SUBDIVISIONS: dict[tuple[str, str], str] = {}

_CODE_RE: t.Final = re.compile(r"[A-Z]{2}(-[A-Z0-9]{1,3})?")

# VAT/VIES prefixes that differ from the ISO 3166-1 code.
_VAT_PREFIX_TO_ISO: t.Final[dict[str, str]] = {"EL": "GR"}


def normalize_country_code(code: str | None) -> str:
    """Upper-case a country code and map VAT-only prefixes (``EL``) to ISO 3166-1."""
    upper = (code or "").strip().upper()
    return _VAT_PREFIX_TO_ISO.get(upper, upper)


def register(code: str, admin_names: t.Iterable[str] = ()) -> t.Callable[[_P], _P]:
    """Class decorator registering a policy for a country or one of its subdivisions.

    Args:
        code: An ISO 3166-1 alpha-2 country (``"ES"``), or an ISO 3166-2 subdivision
            (``"ES-PV"``) whose rules differ from the rest of the country.
        admin_names: For a subdivision, the ``City.admin_name`` spellings that place an
            organization there (see :func:`resolve_org_jurisdiction`).

    Raises:
        ImproperlyConfigured: On a malformed code, a subdivision without admin names (or
            a country with them), or a second policy for the same code.
    """
    key = normalize_country_code(code)
    if not _CODE_RE.fullmatch(key):
        raise ImproperlyConfigured(f"Compliance policy code must be ISO 3166-1 alpha-2 or ISO 3166-2, got {code!r}.")
    names = [name.strip().casefold() for name in admin_names]
    if bool(names) != (len(key) > 2):
        raise ImproperlyConfigured(f"Compliance policy {key}: a subdivision needs admin names, a country takes none.")

    def decorator(cls: _P) -> _P:
        """Record ``cls`` as the policy for ``key``."""
        existing = _REGISTRY.get(key)
        if existing is not None and existing is not cls:
            raise ImproperlyConfigured(f"Two compliance policies for {key}: {existing!r} and {cls!r}.")
        _REGISTRY[key] = cls
        _SUBDIVISIONS.update({(key[:2], name): key for name in names})
        return cls

    return decorator


def registered_policies() -> dict[str, type[CountryCompliancePolicy]]:
    """A copy of the registry (country or subdivision code → policy class)."""
    return dict(_REGISTRY)


def get_policy_for_country(code: str | None) -> CountryCompliancePolicy:
    """The policy for a country or subdivision code (VAT prefixes accepted).

    Tries the exact code, then its country (``ES-NC`` → ``ES`` when no Navarre policy is
    registered), then the EU default.
    """
    key = normalize_country_code(code)
    cls = _REGISTRY.get(key) or _REGISTRY.get(key[:2]) or DefaultEUPolicy
    return cls(key)


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


def resolve_org_jurisdiction(org: "Organization") -> str:
    """The organization's country, or its registered subdivision when its city lies in one.

    A city refines the resolved country only when it is in that same country (an FR VAT
    ID with a Bilbao city stays ``FR``). Select ``city`` on hot paths.

    Returns:
        An ISO 3166-2 code such as ``ES-PV``, else the ISO 3166-1 country, or ``""``.
    """
    country = resolve_org_country(org)
    city = org.city if org.city_id else None
    if not country or city is None or not city.admin_name or city.iso2.upper() != country:
        return country
    return _SUBDIVISIONS.get((country, city.admin_name.strip().casefold()), country)


def get_policy(org: "Organization") -> CountryCompliancePolicy:
    """The policy for an organization's resolved jurisdiction (subdivision or country)."""
    return get_policy_for_country(resolve_org_jurisdiction(org))
