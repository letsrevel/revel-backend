"""Custom-domain hostname normalization and validation (white-label, #1152).

Pure helpers: no model or service imports, so the ``OrganizationDomain`` model can use
them as a field validator. The platform's own hosts are derived from settings at call
time (never cached at import) so ``override_settings`` and per-environment configuration
are honoured.
"""

import ipaddress
import re
from urllib.parse import urlsplit

from django.conf import settings
from django.core.exceptions import ValidationError as DjangoValidationError
from django.utils.translation import gettext as _

MAX_HOSTNAME_LENGTH = 253
MAX_LABEL_LENGTH = 63
_LABEL_RE = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?$")
_LOCALHOST_SUFFIX = ".localhost"


def normalize_hostname(value: str) -> str:
    """Return the canonical form of a hostname: stripped, lowercase, no trailing dot, IDNA (ASCII).

    Only normalizes; it does not validate. A value that cannot be IDNA-encoded is returned
    lowercased, so :func:`validate_custom_hostname` can reject it with a proper message.

    Args:
        value: A raw hostname (no scheme, no port).

    Returns:
        str: The normalized hostname.
    """
    host = value.strip().lower().rstrip(".")
    try:
        return host.encode("idna").decode("ascii")
    except UnicodeError:
        return host


def _registrable_apex(host: str) -> str | None:
    """Return the last two labels of ``host`` (e.g. ``letsrevel.io``), or ``None`` for a single label."""
    # ponytail: naive "last two labels"; wrong for multi-part public suffixes (co.uk), which is fine
    # because it is only applied to the platform's own hosts. Upgrade path: the public suffix list.
    labels = host.split(".")
    return ".".join(labels[-2:]) if len(labels) >= 2 else None


def platform_hosts() -> frozenset[str]:
    """Hostnames a tenant domain must never equal or sit under: the platform's own and their apexes."""
    hosts: set[str] = set()
    for url in (settings.FRONTEND_BASE_URL, settings.BASE_URL):
        host = (urlsplit(url).hostname or "").lower()
        if not host or host == "localhost":
            # ``localhost`` is handled by the ``*.localhost`` rule, not the platform-host rule.
            continue
        hosts.add(host)
        if apex := _registrable_apex(host):
            hosts.add(apex)
    return frozenset(hosts)


def _check_form(host: str) -> None:
    """Reject empty names, wildcards, ports, IP addresses and over-long names."""
    if not host:
        raise DjangoValidationError(_("Enter a hostname."), code="invalid_hostname")
    if "*" in host:
        raise DjangoValidationError(_("Wildcard hostnames are not allowed."), code="wildcard")
    if ":" in host:
        raise DjangoValidationError(_("Enter the hostname without a port."), code="port")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise DjangoValidationError(_("IP addresses are not allowed; use a domain name."), code="ip_address")
    if len(host) > MAX_HOSTNAME_LENGTH:
        raise DjangoValidationError(
            _("Hostname is too long (maximum is {max} characters).").format(max=MAX_HOSTNAME_LENGTH),
            code="too_long",
        )


def _check_labels(host: str) -> None:
    """Require a dotted name of well-formed labels ending in a non-numeric TLD."""
    labels = host.split(".")
    if len(labels) < 2:
        raise DjangoValidationError(_("Enter a fully qualified domain name, for example events.example.com."))
    for label in labels:
        if len(label) > MAX_LABEL_LENGTH:
            raise DjangoValidationError(
                _("Each part of the hostname must be at most {max} characters.").format(max=MAX_LABEL_LENGTH),
                code="label_too_long",
            )
        if not _LABEL_RE.match(label):
            raise DjangoValidationError(_("Hostname contains invalid characters."), code="invalid_hostname")
    if labels[-1].isdigit():
        raise DjangoValidationError(_("Hostname must end in a valid top-level domain."), code="invalid_tld")


def validate_custom_hostname(value: str) -> str:
    """Validate a tenant hostname and return its normalized form.

    Rules: lowercase, no trailing dot, IDNA-encodable, at least one dot, no port, no wildcard,
    not an IP address, at most 253 characters with labels of at most 63, and neither equal to
    nor a subdomain of the platform's own hosts (or their registrable apex). ``*.localhost``
    names are accepted only when ``settings.DEBUG`` is true (dev/E2E).

    Args:
        value: The hostname as entered.

    Returns:
        str: The normalized hostname (what should be stored).

    Raises:
        DjangoValidationError: With a translatable message when the hostname is not acceptable.
    """
    host = normalize_hostname(value)
    _check_form(host)
    _check_labels(host)
    if host.endswith(_LOCALHOST_SUFFIX):
        if not settings.DEBUG:
            raise DjangoValidationError(_("This hostname is reserved for local development."), code="localhost")
        return host
    for platform_host in platform_hosts():
        if host == platform_host or host.endswith(f".{platform_host}"):
            raise DjangoValidationError(_("This hostname belongs to the platform and cannot be used."), code="platform")
    return host


def custom_hostname_validator(value: str) -> None:
    """Django field-validator form of :func:`validate_custom_hostname` (discards the normalized value)."""
    validate_custom_hostname(value)
