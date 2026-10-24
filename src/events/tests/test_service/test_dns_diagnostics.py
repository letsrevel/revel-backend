"""Tests for the DNS pre-flight diagnostics of pending custom domains (#1148). The DNS lookups are faked."""

import typing as t
from unittest.mock import patch

import pytest
from django.core.cache import cache

from events.service import cloudflare_custom_hostnames as cloudflare
from events.service import dns_diagnostics

CNAME_TARGET = "customers.letsrevel.io"
HOST = "events.example.org"
DOUBLED = "events.example.org.example.org"
TXT_LABEL = "_cf-custom-hostname"

Records = dict[tuple[str, str], list[str] | None]


class FakeDns:
    """A canned ``dns_query``: unknown names have no records, ``None`` values simulate a failed lookup."""

    def __init__(self, records: Records) -> None:
        self.records = records
        self.queries: list[tuple[str, str]] = []

    def __call__(self, name: str, rtype: str) -> list[str] | None:
        self.queries.append((name, rtype))
        return self.records.get((name, rtype), [])


@pytest.fixture(autouse=True)
def cname_target(settings: t.Any) -> None:
    settings.CUSTOM_DOMAIN_CNAME_TARGET = CNAME_TARGET


@pytest.fixture
def dns() -> t.Iterator[t.Callable[..., FakeDns]]:
    """Install a fake resolver; ``dns(records)`` starts from a correct CNAME and overlays ``records``."""
    patches: list[t.Any] = []

    def install(records: Records | None = None, *, cname: list[str] | None = None) -> FakeDns:
        fake = FakeDns({(HOST, "CNAME"): [f"{CNAME_TARGET}."] if cname is None else cname, **(records or {})})
        patcher = patch.object(cloudflare, "dns_query", side_effect=fake)
        patcher.start()
        patches.append(patcher)
        return fake

    yield install
    for patcher in patches:
        patcher.stop()


# ---- CNAME ----


def test_a_correct_setup_has_no_hints(dns: t.Callable[..., FakeDns]) -> None:
    dns({("example.org", "NS"): ["ns1.domaincontrol.com."]})

    assert dns_diagnostics.diagnose_dns(HOST) == []  # even though the DNS host is recognised: nothing to fix


def test_cname_with_other_case_or_no_trailing_dot_is_accepted(dns: t.Callable[..., FakeDns]) -> None:
    dns(cname=["Customers.LetsRevel.io"])

    assert dns_diagnostics.diagnose_dns(HOST) == []


def test_missing_cname_says_what_to_add_and_names_the_dns_host(dns: t.Callable[..., FakeDns]) -> None:
    dns({("example.org", "NS"): ["ns1.domaincontrol.com."]}, cname=[])

    assert dns_diagnostics.diagnose_dns(HOST) == [
        f"No CNAME record was found for {HOST}; add one pointing to {CNAME_TARGET}.",
        "Your DNS is hosted at GoDaddy. How to add the records: https://www.godaddy.com/help/add-a-cname-record-19236",
    ]


@pytest.mark.parametrize("rtype", ["A", "AAAA"])
def test_address_records_without_a_cname_say_so(dns: t.Callable[..., FakeDns], rtype: str) -> None:
    dns({(HOST, rtype): ["203.0.113.7"]}, cname=[])

    assert dns_diagnostics.diagnose_dns(HOST) == [
        f"{HOST} has A or AAAA records but no CNAME; replace them with a CNAME record pointing to "
        f"{CNAME_TARGET} (a proxied, ALIAS or flattened record may not validate)."
    ]


def test_a_cname_pointing_elsewhere_says_where(dns: t.Callable[..., FakeDns]) -> None:
    dns(cname=["Other.Example.net."])

    assert dns_diagnostics.diagnose_dns(HOST) == [
        f"The CNAME record for {HOST} points to other.example.net, not to {CNAME_TARGET}."
    ]


def test_the_cname_check_is_skipped_without_a_configured_target(dns: t.Callable[..., FakeDns], settings: t.Any) -> None:
    settings.CUSTOM_DOMAIN_CNAME_TARGET = ""
    fake = dns(cname=[])

    assert dns_diagnostics.diagnose_dns(HOST) == []
    assert (HOST, "CNAME") not in fake.queries


# ---- doubled name ----


@pytest.mark.parametrize(
    "records",
    [
        {(DOUBLED, "CNAME"): [f"{CNAME_TARGET}."]},
        {(f"{TXT_LABEL}.{DOUBLED}", "TXT"): ['"owner-token"']},
    ],
)
def test_records_at_the_doubled_name_are_reported(dns: t.Callable[..., FakeDns], records: Records) -> None:
    dns(records, cname=[])

    hints = dns_diagnostics.diagnose_dns(HOST)

    assert (
        f"Records were found at {DOUBLED}, so your DNS provider appends your domain to the record name; "
        f"enter only events for the CNAME and {TXT_LABEL}.events for the TXT record."
    ) in hints


def test_nothing_at_the_doubled_name_gives_no_doubled_hint(dns: t.Callable[..., FakeDns]) -> None:
    dns()

    assert dns_diagnostics.diagnose_dns(HOST) == []


def test_doubled_name_hint_for_a_deeper_subdomain(dns: t.Callable[..., FakeDns]) -> None:
    dns({("shop.events.example.org.example.org", "CNAME"): [f"{CNAME_TARGET}."]})

    hints = dns_diagnostics.diagnose_dns("shop.events.example.org")

    assert any("enter only shop.events for the CNAME and _cf-custom-hostname.shop.events" in h for h in hints)


# ---- CAA ----


def caa(*records: str, name: str = HOST) -> Records:
    return {(name, "CAA"): list(records)}


def test_no_caa_records_is_fine(dns: t.Callable[..., FakeDns]) -> None:
    fake = dns()

    assert dns_diagnostics.diagnose_dns(HOST) == []
    assert [q for q in fake.queries if q[1] == "CAA"] == [(HOST, "CAA"), ("example.org", "CAA")]  # up to the apex


@pytest.mark.parametrize(
    "records",
    [
        ['0 issue "letsencrypt.org"'],
        ['0 issue "pki.goog"', '0 issue "LetsEncrypt.org; validationmethods=http-01"'],
        ['0 issue "letsencrypt.org"', '0 issue "pki.goog; cansignhttpexchanges=yes"', '0 issue "ssl.com"'],
        ['0 iodef "mailto:security@example.org"'],  # no issue records: any CA may issue
        ['0 issuewild "pki.goog"'],  # only constrains wildcard certificates
    ],
)
def test_caa_that_allows_lets_encrypt_is_fine(dns: t.Callable[..., FakeDns], records: list[str]) -> None:
    dns(caa(*records))

    assert dns_diagnostics.diagnose_dns(HOST) == []


@pytest.mark.parametrize(
    "records", [['0 issue "pki.goog"'], ['0 issue ";"'], ['0 issue "digicert.com"', '0 issue "ssl.com"']]
)
def test_caa_that_blocks_lets_encrypt_says_which_values_to_add(
    dns: t.Callable[..., FakeDns], records: list[str]
) -> None:
    dns(caa(*records))

    assert dns_diagnostics.diagnose_dns(HOST) == [
        f"The CAA records at {HOST} do not allow Let's Encrypt, so Revel cannot get a certificate; add "
        "CAA issue records for letsencrypt.org, pki.goog, ssl.com, or remove the CAA records."
    ]


def test_caa_is_inherited_from_the_first_parent_that_has_any(dns: t.Callable[..., FakeDns]) -> None:
    dns(caa('0 issue "pki.goog"', name="example.org"))

    assert "The CAA records at example.org do not allow Let's Encrypt" in dns_diagnostics.diagnose_dns(HOST)[0]


def test_caa_at_the_hostname_overrides_a_blocking_parent(dns: t.Callable[..., FakeDns]) -> None:
    dns({**caa('0 issue "letsencrypt.org"'), **caa('0 issue "pki.goog"', name="example.org")})

    assert dns_diagnostics.diagnose_dns(HOST) == []


def test_caa_lookup_failure_skips_the_check(dns: t.Callable[..., FakeDns]) -> None:
    dns({(HOST, "CAA"): None, ("example.org", "CAA"): ['0 issue "pki.goog"']})

    assert dns_diagnostics.diagnose_dns(HOST) == []


# ---- apex ----


def test_a_root_domain_suggests_a_subdomain(dns: t.Callable[..., FakeDns]) -> None:
    dns({("example.org", "CNAME"): [f"{CNAME_TARGET}."]})

    assert dns_diagnostics.diagnose_dns("example.org") == [
        "example.org is a root domain, and most DNS providers cannot put a CNAME on one; "
        "use a subdomain such as events.example.org."
    ]


def test_a_subdomain_does_not_get_the_root_domain_hint(dns: t.Callable[..., FakeDns]) -> None:
    dns(cname=[])

    assert not any("root domain" in hint for hint in dns_diagnostics.diagnose_dns(HOST))


# ---- DNS provider ----

PROVIDER_NAMESERVERS = [
    ("Cloudflare", "bob.ns.cloudflare.com."),
    ("GoDaddy", "ns57.domaincontrol.com."),
    ("IONOS", "ns1086.ui-dns.com."),
    ("IONOS", "ns1086.ui-dns.de."),
    ("Namecheap", "dns1.registrar-servers.com."),
    ("Amazon Route 53", "ns-1536.awsdns-00.co.uk."),
    ("OVHcloud", "dns100.ovh.net."),
    ("Gandi", "ns-153-a.gandi.net."),
    ("Squarespace", "ns-cloud-a1.squarespacedns.com."),
    ("Hetzner", "hydrogen.ns.hetzner.com."),
    ("Hetzner", "ns1.your-server.de."),
    ("STRATO", "docks12.rzone.de."),
    ("netcup", "root-dns.netcup-dns.com."),
    ("DigitalOcean", "ns1.digitalocean.com."),
    ("Vercel", "ns1.vercel-dns.com."),
    ("Wix", "ns10.wixdns.net."),
]


@pytest.mark.parametrize(("name", "nameserver"), PROVIDER_NAMESERVERS)
def test_the_dns_host_is_recognised_from_its_nameservers(
    dns: t.Callable[..., FakeDns], name: str, nameserver: str
) -> None:
    dns({("example.org", "NS"): ["ns.unknown.example.", nameserver]}, cname=[])

    hints = dns_diagnostics.diagnose_dns(HOST)

    provider = next(p for p in dns_diagnostics.DNS_PROVIDERS if p.name == name)
    assert f"Your DNS is hosted at {name}. How to add the records: {provider.help_url}" in hints


def test_every_provider_has_a_unique_marker_and_an_https_help_url() -> None:
    markers = [p.nameserver_marker for p in dns_diagnostics.DNS_PROVIDERS]
    assert len(markers) == len(set(markers))
    assert all(p.help_url.startswith("https://") for p in dns_diagnostics.DNS_PROVIDERS)


def test_cloudflare_dns_adds_the_dns_only_caveat(dns: t.Callable[..., FakeDns]) -> None:
    dns({("example.org", "NS"): ["bob.ns.cloudflare.com.", "alice.ns.cloudflare.com."]}, cname=[])

    hints = dns_diagnostics.diagnose_dns(HOST)

    assert hints[-1] == dns_diagnostics.CLOUDFLARE_DNS_ONLY_HINT
    assert "DNS only (grey cloud)" in hints[-1]
    assert hints[-2].startswith("Your DNS is hosted at Cloudflare. ")


def test_other_dns_hosts_get_no_cloudflare_caveat(dns: t.Callable[..., FakeDns]) -> None:
    dns({("example.org", "NS"): ["ns57.domaincontrol.com."]}, cname=[])

    assert dns_diagnostics.CLOUDFLARE_DNS_ONLY_HINT not in dns_diagnostics.diagnose_dns(HOST)


@pytest.mark.parametrize("ns", [[], None, ["ns1.some-small-host.example."]])
def test_an_unknown_or_unreadable_dns_host_adds_no_provider_hint(
    dns: t.Callable[..., FakeDns], ns: list[str] | None
) -> None:
    dns({("example.org", "NS"): ns}, cname=[])

    hints = dns_diagnostics.diagnose_dns(HOST)

    assert len(hints) == 1
    assert not any("Your DNS is hosted" in hint for hint in hints)


# ---- lookup failures ----


def test_an_unreachable_resolver_costs_one_query_and_no_hints(dns: t.Callable[..., FakeDns]) -> None:
    fake = dns()
    fake.records[(HOST, "CNAME")] = None

    assert dns_diagnostics.diagnose_dns(HOST) == []
    assert fake.queries == [(HOST, "CNAME")]


def test_failing_secondary_lookups_never_raise(dns: t.Callable[..., FakeDns]) -> None:
    dns(
        {
            (HOST, "A"): None,
            (HOST, "AAAA"): None,
            (DOUBLED, "CNAME"): None,
            (f"{TXT_LABEL}.{DOUBLED}", "TXT"): None,
            (HOST, "CAA"): None,
            ("example.org", "NS"): None,
        },
        cname=[],
    )

    assert dns_diagnostics.diagnose_dns(HOST) == [
        f"No CNAME record was found for {HOST}; add one pointing to {CNAME_TARGET}."
    ]


# ---- ownership_only (Cloudflare is ready, only the ownership TXT is missing) ----


def test_ownership_only_reports_a_txt_record_at_the_doubled_name(dns: t.Callable[..., FakeDns]) -> None:
    fake = dns(
        {(f"{TXT_LABEL}.{DOUBLED}", "TXT"): ['"owner-token"'], ("example.org", "NS"): ["ns57.domaincontrol.com."]}
    )

    hints = dns_diagnostics.diagnose_dns(HOST, ownership_only=True)

    assert hints[0].startswith(f"Records were found at {DOUBLED}")
    assert hints[1].startswith("Your DNS is hosted at GoDaddy.")
    assert {rtype for _, rtype in fake.queries} == {"CNAME", "TXT", "NS"}  # no CAA, A or AAAA lookups
    assert (HOST, "CNAME") not in fake.queries


def test_ownership_only_is_silent_when_nothing_is_at_the_doubled_name(dns: t.Callable[..., FakeDns]) -> None:
    dns({("example.org", "NS"): ["ns57.domaincontrol.com."]}, cname=[])

    assert dns_diagnostics.diagnose_dns(HOST, ownership_only=True) == []


# ---- detect_dns_provider (cached, for the custom-domain API) ----

DETECT_APEXES = (
    "detect-known.example",
    "detect-unknown.example",
    "detect-cache.example",
    "detect-failed.example",
    "detect-cache-down.example",
)
DETECT_APEX = "detect-apex.example"


@pytest.fixture
def detect_cache() -> t.Iterator[None]:
    """Drop this module's cache keys (never ``cache.clear()``: xdist workers share one cache)."""
    keys = [f"dns_provider:{apex}" for apex in (*DETECT_APEXES, DETECT_APEX)]
    cache.delete_many(keys)
    yield
    cache.delete_many(keys)


@pytest.mark.usefixtures("detect_cache")
def test_detect_dns_provider_recognises_a_provider() -> None:
    with patch.object(cloudflare, "dns_query", return_value=["ns57.domaincontrol.com."]):
        provider = dns_diagnostics.detect_dns_provider("events.detect-known.example")

    assert provider is not None
    assert provider.name == "GoDaddy"
    assert provider.help_url.startswith("https://")


@pytest.mark.usefixtures("detect_cache")
def test_detect_dns_provider_returns_none_for_unknown_nameservers() -> None:
    with patch.object(cloudflare, "dns_query", return_value=["ns1.some-small-host.example."]):
        assert dns_diagnostics.detect_dns_provider("events.detect-unknown.example") is None


@pytest.mark.usefixtures("detect_cache")
def test_detect_dns_provider_queries_the_ns_of_the_apex() -> None:
    with patch.object(cloudflare, "dns_query", return_value=["ns1.registrar-servers.com."]) as query:
        provider = dns_diagnostics.detect_dns_provider(f"events.{DETECT_APEX}")

    query.assert_called_once_with(DETECT_APEX, "NS")
    assert provider is not None
    assert provider.name == "Namecheap"


@pytest.mark.usefixtures("detect_cache")
def test_detect_dns_provider_caches_a_completed_lookup_for_an_hour() -> None:
    hostname = "events.detect-cache.example"
    with (
        patch.object(cloudflare, "dns_query", return_value=["ns1.ovh.net."]) as query,
        patch.object(cache, "set", wraps=cache.set) as cache_set,
    ):
        first = dns_diagnostics.detect_dns_provider(hostname)
        second = dns_diagnostics.detect_dns_provider(hostname)

    assert first is not None and first == second
    assert first.name == "OVHcloud"
    query.assert_called_once()
    cache_set.assert_called_once_with(
        "dns_provider:detect-cache.example", "OVHcloud", dns_diagnostics.DNS_PROVIDER_CACHE_TTL
    )
    assert dns_diagnostics.DNS_PROVIDER_CACHE_TTL == 60 * 60


@pytest.mark.usefixtures("detect_cache")
def test_detect_dns_provider_caches_no_known_provider_for_an_hour() -> None:
    hostname = "events.detect-unknown.example"
    with (
        patch.object(cloudflare, "dns_query", return_value=["ns1.some-small-host.example."]) as query,
        patch.object(cache, "set", wraps=cache.set) as cache_set,
    ):
        assert dns_diagnostics.detect_dns_provider(hostname) is None
        assert dns_diagnostics.detect_dns_provider(hostname) is None

    query.assert_called_once()
    cache_set.assert_called_once_with("dns_provider:detect-unknown.example", "", dns_diagnostics.DNS_PROVIDER_CACHE_TTL)


@pytest.mark.usefixtures("detect_cache")
def test_detect_dns_provider_caches_a_failed_lookup_briefly() -> None:
    hostname = "events.detect-failed.example"
    with (
        patch.object(cloudflare, "dns_query", return_value=None) as query,
        patch.object(cache, "set", wraps=cache.set) as cache_set,
    ):
        assert dns_diagnostics.detect_dns_provider(hostname) is None
        assert dns_diagnostics.detect_dns_provider(hostname) is None

    query.assert_called_once()  # the outage costs one lookup, not one per call
    cache_set.assert_called_once_with(
        "dns_provider:detect-failed.example", "", dns_diagnostics.DNS_PROVIDER_FAILED_CACHE_TTL
    )
    assert dns_diagnostics.DNS_PROVIDER_FAILED_CACHE_TTL == 5 * 60


@pytest.mark.usefixtures("detect_cache")
def test_detect_dns_provider_fails_open_when_the_cache_read_fails() -> None:
    with (
        patch.object(cache, "get", side_effect=ConnectionError("redis down")),
        patch.object(cloudflare, "dns_query", return_value=["ns1.ovh.net."]) as query,
    ):
        provider = dns_diagnostics.detect_dns_provider("events.detect-cache-down.example")

    query.assert_called_once()  # a failed read counts as a miss
    assert provider is not None
    assert provider.name == "OVHcloud"


@pytest.mark.usefixtures("detect_cache")
def test_detect_dns_provider_fails_open_when_the_cache_write_fails() -> None:
    with (
        patch.object(cache, "set", side_effect=ConnectionError("redis down")),
        patch.object(cloudflare, "dns_query", return_value=["ns1.ovh.net."]),
    ):
        provider = dns_diagnostics.detect_dns_provider("events.detect-cache-down.example")

    assert provider is not None
    assert provider.name == "OVHcloud"
