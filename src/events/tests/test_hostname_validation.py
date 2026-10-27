"""Tests for the custom-domain hostname helpers (#1152)."""

import typing as t

import pytest
from django.core.exceptions import ValidationError

from common.models import SiteSettings
from events.utils.hostname import normalize_hostname, validate_custom_hostname

pytestmark = pytest.mark.django_db  # ``platform_hosts`` reads the SiteSettings singleton


@pytest.fixture(autouse=True)
def platform_urls(settings: t.Any) -> None:
    """Pin the platform hosts so the tests do not depend on the local .env."""
    settings.FRONTEND_BASE_URL = "https://app.letsrevel.io"
    settings.BASE_URL = "https://api.letsrevel.io"
    settings.DEBUG = False


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Events.Example.COM", "events.example.com"),
        ("events.example.com.", "events.example.com"),
        ("  events.example.com ", "events.example.com"),
        ("bücher.example", "xn--bcher-kva.example"),
        ("xn--bcher-kva.example", "xn--bcher-kva.example"),
    ],
)
def test_valid_hostnames_are_normalized(raw: str, expected: str) -> None:
    """Valid input comes back lowercase, dot-less and punycoded."""
    assert validate_custom_hostname(raw) == expected


def test_normalize_does_not_raise_on_unencodable_input() -> None:
    """Normalization alone never raises; validation reports the problem."""
    assert normalize_hostname("A" * 70 + ".example") == "a" * 70 + ".example"


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "   ",
        "localhost",  # single label
        "example",  # single label
        "events.example.com:8443",  # port
        "*.example.com",  # wildcard
        "192.168.0.1",  # IPv4
        "2001:db8::1",  # IPv6
        "[::1]",
        "1.2.3",  # numeric TLD
        "exa mple.com",
        "ex_ample.com",
        "https://example.com",
        "example.com/path",
        "-bad.example.com",
        "bad-.example.com",
        "a..example.com",
        ("a" * 64) + ".example.com",  # label too long
        ".".join(["abcdefghij"] * 26) + ".com",  # > 253 chars
    ],
)
def test_invalid_hostnames_are_rejected(bad: str) -> None:
    """Ports, wildcards, IPs, malformed labels and over-long names are refused."""
    with pytest.raises(ValidationError):
        validate_custom_hostname(bad)


@pytest.mark.parametrize(
    "platform",
    [
        "app.letsrevel.io",  # frontend host
        "api.letsrevel.io",  # backend host
        "letsrevel.io",  # registrable apex
        "anything.letsrevel.io",  # subdomain of the apex
        "deep.app.letsrevel.io",
        "APP.LetsRevel.io.",
    ],
)
def test_platform_hosts_and_subdomains_are_rejected(platform: str) -> None:
    """A tenant can never claim the platform's own hosts or anything beneath them."""
    with pytest.raises(ValidationError):
        validate_custom_hostname(platform)


def test_lookalike_of_platform_host_is_allowed() -> None:
    """Suffix matching is label-aware: notletsrevel.io is somebody else's domain."""
    assert validate_custom_hostname("notletsrevel.io") == "notletsrevel.io"
    assert validate_custom_hostname("letsrevel.io.example.com") == "letsrevel.io.example.com"


def test_localhost_hostnames_only_allowed_in_debug(settings: t.Any) -> None:
    """``*.localhost`` is for dev/E2E and is refused outside DEBUG."""
    settings.DEBUG = True
    assert validate_custom_hostname("tenant.localhost") == "tenant.localhost"
    assert validate_custom_hostname("a.b.localhost") == "a.b.localhost"
    with pytest.raises(ValidationError):
        validate_custom_hostname("localhost")  # still needs a dot

    settings.DEBUG = False
    with pytest.raises(ValidationError):
        validate_custom_hostname("tenant.localhost")


def test_localhost_platform_urls_do_not_block_tenant_localhost(settings: t.Any) -> None:
    """In dev the platform URLs are localhost; that must not swallow ``tenant.localhost``."""
    settings.DEBUG = True
    settings.FRONTEND_BASE_URL = "http://localhost:5173"
    settings.BASE_URL = "http://localhost:8000"
    assert validate_custom_hostname("tenant.localhost") == "tenant.localhost"
    assert validate_custom_hostname("events.example.com") == "events.example.com"


def test_fallback_origin_cannot_be_claimed(settings: t.Any) -> None:
    """``CUSTOM_DOMAIN_CNAME_TARGET`` is the Cloudflare fallback origin, even on an unrelated domain."""
    settings.CUSTOM_DOMAIN_CNAME_TARGET = "fallback.cdn-example.net"

    for host in ("fallback.cdn-example.net", "x.fallback.cdn-example.net"):
        with pytest.raises(ValidationError):
            validate_custom_hostname(host)
    # Only the target itself is reserved, not its whole (possibly shared) apex.
    assert validate_custom_hostname("tenant.cdn-example.net") == "tenant.cdn-example.net"


def test_unset_fallback_origin_blocks_nothing(settings: t.Any) -> None:
    settings.CUSTOM_DOMAIN_CNAME_TARGET = ""
    assert validate_custom_hostname("events.example.com") == "events.example.com"


def test_site_settings_frontend_host_is_a_platform_host(settings: t.Any) -> None:
    """Links use ``SiteSettings.frontend_base_url``, so its host (and apex) is reserved too."""
    site = SiteSettings.get_solo()
    site.frontend_base_url = "https://app.revel-example.org"
    site.save()

    for host in ("app.revel-example.org", "revel-example.org", "x.revel-example.org"):
        with pytest.raises(ValidationError):
            validate_custom_hostname(host)


@pytest.mark.parametrize("host", ["letsrevel.io", "www.letsrevel.io", "api.letsrevel.io", "any.letsrevel.io"])
def test_prod_like_platform_hosts_are_rejected(settings: t.Any, host: str) -> None:
    settings.FRONTEND_BASE_URL = "https://letsrevel.io"
    settings.BASE_URL = "https://api.letsrevel.io"
    settings.DEBUG = False
    site = SiteSettings.get_solo()
    site.frontend_base_url = "https://letsrevel.io"
    site.save()

    with pytest.raises(ValidationError):
        validate_custom_hostname(host)


@pytest.fixture
def co_uk_platform(settings: t.Any) -> None:
    """A self-hosted platform under the multi-part public suffix ``co.uk`` (#1177)."""
    settings.FRONTEND_BASE_URL = "https://revel.example.co.uk"
    settings.BASE_URL = "https://api.revel.example.co.uk"
    site = SiteSettings.get_solo()
    site.frontend_base_url = "https://revel.example.co.uk"
    site.save()


@pytest.mark.usefixtures("co_uk_platform")
def test_multi_part_suffix_platform_without_override_reserves_the_suffix(settings: t.Any) -> None:
    """Default behaviour is unchanged: the derived apex is the last two labels, i.e. ``co.uk``."""
    settings.PLATFORM_APEX_DOMAINS = ()
    with pytest.raises(ValidationError):
        validate_custom_hostname("events.othersite.co.uk")


@pytest.mark.usefixtures("co_uk_platform")
def test_platform_apex_domains_fixes_multi_part_suffix_platforms(settings: t.Any) -> None:
    """``PLATFORM_APEX_DOMAINS`` replaces the derived apex: other co.uk sites are fine, the platform zone is not."""
    settings.PLATFORM_APEX_DOMAINS = ("example.co.uk",)

    assert validate_custom_hostname("events.othersite.co.uk") == "events.othersite.co.uk"
    for host in ("x.example.co.uk", "example.co.uk", "revel.example.co.uk", "api.revel.example.co.uk"):
        with pytest.raises(ValidationError):
            validate_custom_hostname(host)


@pytest.mark.usefixtures("co_uk_platform")
def test_platform_apex_domains_ignores_malformed_entries(settings: t.Any) -> None:
    """Empty and malformed entries reserve nothing; if none is valid the derived apex applies."""
    settings.PLATFORM_APEX_DOMAINS = ("", "co", "bad domain.co.uk", "https://x.co.uk", "-a.co.uk", "Example.CO.UK.")
    assert validate_custom_hostname("events.othersite.co.uk") == "events.othersite.co.uk"
    with pytest.raises(ValidationError):
        validate_custom_hostname("x.example.co.uk")  # the one valid entry (normalized) still applies

    settings.PLATFORM_APEX_DOMAINS = ("", "co", "bad domain.co.uk")
    with pytest.raises(ValidationError):
        validate_custom_hostname("events.othersite.co.uk")  # nothing valid: back to the derived ``co.uk``
