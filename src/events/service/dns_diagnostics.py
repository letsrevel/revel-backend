"""DNS pre-flight diagnostics for PENDING white-label custom domains (#1148).

The top expected support tickets are DNS mistakes, so :func:`diagnose_dns` looks at the customer's public DNS
(over DNS-over-HTTPS, see :func:`events.service.cloudflare_custom_hostnames.dns_query`) and turns what it
finds into plain-English hints. ``sync_domain_status`` appends them to ``OrganizationDomain.last_errors``,
the same place Cloudflare's own messages go, so the organization owner sees exactly what to fix.

Everything here is advisory and fail-safe: a lookup that fails skips its check (logged by the lookup), nothing
raises, and nothing gates activation (the ownership TXT gate stays in ``custom_domain_service``).
"""

import re
import typing as t

from django.conf import settings
from django.core.cache import cache

from events.service import cloudflare_custom_hostnames as cloudflare
from events.utils.hostname import registrable_apex

# Cloudflare names the ownership TXT record ``_cf-custom-hostname.<hostname>``.
OWNERSHIP_TXT_LABEL = "_cf-custom-hostname"

# CAs that must be allowed when the customer publishes CAA records. Cloudflare for SaaS issues custom-hostname
# certificates from Let's Encrypt, Google Trust Services (``pki.goog``) or SSL.com
# (https://developers.cloudflare.com/cloudflare-for-platforms/cloudflare-for-saas/security/certificate-management/issue-and-validate/validate-certificates/troubleshooting/#caa-records
# and https://developers.cloudflare.com/ssl/reference/certificate-authorities/#caa-records), and Caddy gets the
# origin certificates from Let's Encrypt. The hint triggers only when Let's Encrypt is blocked (it covers both
# the origin and, as a Cloudflare option, the edge) but lists all three, as Cloudflare documents them.
LETS_ENCRYPT_CA = "letsencrypt.org"
CAA_CAS_TO_ALLOW = (LETS_ENCRYPT_CA, "pki.goog", "ssl.com")
_CAA_ISSUE = re.compile(r'^\d+\s+issue\s+"([^"]*)"\s*$', re.IGNORECASE)


class DnsProvider(t.NamedTuple):
    """A DNS host recognised by (a fragment of) its nameserver names, with its "add a record" help page."""

    nameserver_marker: str
    name: str
    help_url: str


# The first entry whose marker occurs in one of the zone's nameserver names wins. Every URL was fetched and
# checked to exist and explain adding DNS records.
DNS_PROVIDERS: tuple[DnsProvider, ...] = (
    DnsProvider(
        "ns.cloudflare.com",
        "Cloudflare",
        "https://developers.cloudflare.com/dns/manage-dns-records/how-to/create-dns-records/",
    ),
    DnsProvider("domaincontrol.com", "GoDaddy", "https://www.godaddy.com/help/add-a-cname-record-19236"),
    DnsProvider(
        "ui-dns.",
        "IONOS",
        "https://www.ionos.com/help/domains/configuring-cname-records-for-subdomains/configuring-a-cname-record-for-a-subdomain/",
    ),
    DnsProvider(
        "registrar-servers.com",
        "Namecheap",
        "https://www.namecheap.com/support/knowledgebase/article.aspx/9646/2237/how-to-create-a-cname-record-for-your-domain/",
    ),
    DnsProvider(
        "awsdns-",
        "Amazon Route 53",
        "https://docs.aws.amazon.com/Route53/latest/DeveloperGuide/resource-record-sets-creating.html",
    ),
    DnsProvider("ovh.net", "OVHcloud", "https://docs.ovhcloud.com/en/guides/web-cloud/domains/dns-zone-edit"),
    DnsProvider("gandi.net", "Gandi", "https://docs.gandi.net/en/domain_names/common_operations/dns_records.html"),
    DnsProvider(
        "squarespacedns.com",
        "Squarespace",
        "https://support.squarespace.com/hc/en-us/articles/31119879125645-DNS-records-for-web-hosting",
    ),
    DnsProvider(
        "ns.hetzner.", "Hetzner", "https://docs.hetzner.com/dns-console/dns/manage-records/managing-cname-records/"
    ),
    DnsProvider(
        "your-server.de", "Hetzner", "https://docs.hetzner.com/dns-console/dns/manage-records/managing-cname-records/"
    ),
    DnsProvider(
        "rzone.de", "STRATO", "https://www.strato.de/faq/domains/wie-kann-ich-bei-strato-meine-dns-eintraege-verwalten/"
    ),
    DnsProvider(
        "netcup-dns.", "netcup", "https://www.netcup.com/en/helpcenter/documentation/web-hosting/webhosting-dns"
    ),
    DnsProvider(
        "digitalocean.com",
        "DigitalOcean",
        "https://docs.digitalocean.com/products/networking/dns/how-to/manage-records/",
    ),
    DnsProvider("vercel-dns.com", "Vercel", "https://vercel.com/docs/domains/managing-dns-records"),
    DnsProvider(
        "wixdns.net", "Wix", "https://support.wix.com/en/article/adding-or-updating-cname-records-in-your-wix-account"
    ),
)

# ``detect_dns_provider`` cache lifetimes: a completed lookup is stable; a failed one is retried soon.
DNS_PROVIDER_CACHE_TTL = 60 * 60
DNS_PROVIDER_FAILED_CACHE_TTL = 5 * 60

# Cloudflare: O2O (a proxied record in a customer zone that points at a Cloudflare for SaaS provider) only applies
# across different Cloudflare accounts, and the customer zone's settings apply to the traffic first
# (https://developers.cloudflare.com/cloudflare-for-platforms/cloudflare-for-saas/saas-customers/how-it-works/).
CLOUDFLARE_DNS_ONLY_HINT = (
    "On Cloudflare, set the CNAME to DNS only (grey cloud): a proxied (orange cloud) record applies your own "
    "Cloudflare settings before Revel's and only works across different Cloudflare accounts."
)


def _normalize(target: str) -> str:
    return target.strip().lower().rstrip(".")


def _cname_hints(hostname: str, target: str) -> list[str] | None:
    """Check the CNAME at ``hostname``; ``None`` when the lookup failed."""
    if (found := cloudflare.dns_query(hostname, "CNAME")) is None:
        return None
    if not found:
        addresses = [*(cloudflare.dns_query(hostname, "A") or []), *(cloudflare.dns_query(hostname, "AAAA") or [])]
        if addresses:
            return [
                f"{hostname} has A or AAAA records but no CNAME; replace them with a CNAME record pointing to "
                f"{target} (a proxied, ALIAS or flattened record may not validate)."
            ]
        return [f"No CNAME record was found for {hostname}; add one pointing to {target}."]
    if target not in map(_normalize, found):
        return [f"The CNAME record for {hostname} points to {', '.join(map(_normalize, found))}, not to {target}."]
    return []


def _doubled_name_hints(hostname: str, apex: str) -> list[str]:
    """Detect records created at ``<hostname>.<apex>``: the DNS UI appended the domain to the name typed in."""
    doubled = f"{hostname}.{apex}"
    cname = cloudflare.dns_query(doubled, "CNAME")
    txt = cloudflare.dns_query(f"{OWNERSHIP_TXT_LABEL}.{doubled}", "TXT")
    if not (cname or txt):
        return []
    name = hostname.removesuffix(f".{apex}") if hostname != apex else "@"
    txt_name = OWNERSHIP_TXT_LABEL if name == "@" else f"{OWNERSHIP_TXT_LABEL}.{name}"
    return [
        f"Records were found at {doubled}, so your DNS provider appends your domain to the record name; "
        f"enter only {name} for the CNAME and {txt_name} for the TXT record."
    ]


def _caa_hints(hostname: str, apex: str) -> list[str]:
    """Check the CAA records that apply to ``hostname`` (RFC 8659: the first level, going up, that has any)."""
    labels = hostname.split(".")
    apex_depth = len(apex.split("."))
    for depth in range(len(labels), apex_depth - 1, -1):
        name = ".".join(labels[-depth:])
        if (records := cloudflare.dns_query(name, "CAA")) is None:
            return []
        if not records:
            continue
        issuers = [match.group(1).split(";")[0].strip().lower() for r in records if (match := _CAA_ISSUE.match(r))]
        if issuers and LETS_ENCRYPT_CA not in issuers:
            return [
                f"The CAA records at {name} do not allow Let's Encrypt, so Revel cannot get a certificate; add "
                f"CAA issue records for {', '.join(CAA_CAS_TO_ALLOW)}, or remove the CAA records."
            ]
        return []
    return []


def _match_provider(nameservers: list[str]) -> DnsProvider | None:
    """Return the first known DNS host that one of ``nameservers`` belongs to."""
    for nameserver in nameservers:
        for provider in DNS_PROVIDERS:
            if provider.nameserver_marker in nameserver.lower():
                return provider
    return None


def _provider_hints(apex: str) -> list[str]:
    """Name the DNS host of ``apex`` (from its NS records) and link its "add a record" help page."""
    if (provider := _match_provider(cloudflare.dns_query(apex, "NS") or [])) is None:
        return []
    hints = [f"Your DNS is hosted at {provider.name}. How to add the records: {provider.help_url}"]
    return [*hints, CLOUDFLARE_DNS_ONLY_HINT] if provider.name == "Cloudflare" else hints


def detect_dns_provider(hostname: str) -> DnsProvider | None:
    """Return the DNS host of ``hostname``'s registrable domain (from its NS records), if it is a known one.

    Cached per apex: it runs while the custom-domain API response is built and the page polls that endpoint, so
    an uncached lookup would cost a DNS-over-HTTPS round trip per page view. A completed lookup (including "no
    known provider") is cached for an hour; a failed one for five minutes, so a resolver outage costs at most
    one timeout per apex per five minutes. Never raises.

    Args:
        hostname: The normalized custom hostname.

    Returns:
        DnsProvider | None: The matching provider, or ``None`` (unknown provider or the lookup failed).
    """
    apex = registrable_apex(hostname) or hostname
    key = f"dns_provider:{apex}"
    if (cached := cache.get(key)) is not None:  # "" is the cached "no known provider"
        return next((p for p in DNS_PROVIDERS if p.name == cached), None)
    nameservers = cloudflare.dns_query(apex, "NS")
    provider = _match_provider(nameservers or [])
    cache.set(
        key,
        provider.name if provider else "",
        DNS_PROVIDER_FAILED_CACHE_TTL if nameservers is None else DNS_PROVIDER_CACHE_TTL,
    )
    return provider


def diagnose_dns(hostname: str, *, ownership_only: bool = False) -> list[str]:
    """Return plain-English hints about DNS mistakes that keep a PENDING custom domain from activating.

    Checks the CNAME (missing, A/AAAA records instead, wrong target), records created at the doubled name
    (``events.example.org.example.org``), CAA records that block Let's Encrypt, and a root-domain hostname.
    When any of them finds something, the hints end with the customer's DNS host and a link to its
    "add a record" help page (only then: a domain that is merely waiting for Cloudflare gets no noise).

    Costs about 6 to 9 DNS-over-HTTPS queries. If the resolver cannot be reached for the first one, the rest
    is skipped, so an outage costs one timeout, not nine. Never raises.

    Args:
        hostname: The normalized custom hostname.
        ownership_only: Cloudflare already reports the hostname ready and only the ownership TXT record is
            missing: check only the one mistake that explains it (records at the doubled name).

    Returns:
        list[str]: The hints (empty when nothing is wrong or nothing could be checked).
    """
    # ponytail: the naive registrable domain (last two labels) is wrong under a multi-part public suffix such as
    # co.uk (#1177): the doubled-name check then misses and the NS lookup finds the registry, so no provider hint.
    apex = registrable_apex(hostname) or hostname
    if ownership_only:
        hints = _doubled_name_hints(hostname, apex)
    else:
        target = _normalize(str(settings.CUSTOM_DOMAIN_CNAME_TARGET))
        cname_hints = _cname_hints(hostname, target) if target else []
        if cname_hints is None:  # the resolver is unreachable: do not spend a timeout per remaining query
            return []
        apex_hints = (
            [
                f"{hostname} is a root domain, and most DNS providers cannot put a CNAME on one; "
                f"use a subdomain such as events.{hostname}."
            ]
            if hostname == apex
            else []
        )
        hints = [*apex_hints, *cname_hints, *_doubled_name_hints(hostname, apex), *_caa_hints(hostname, apex)]
    return [*hints, *_provider_hints(apex)] if hints else []
