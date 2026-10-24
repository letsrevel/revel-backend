"""Org-admin custom-domain schemas (white-label, #1151)."""

import enum
import typing as t

from django.conf import settings
from ninja import Schema
from pydantic import AwareDatetime, Field

from events.models import OrganizationDomain, OrganizationDomainStatus
from events.service import dns_diagnostics


class DnsRecordType(enum.StrEnum):
    """DNS record types an organization may be asked to create."""

    CNAME = "CNAME"
    TXT = "TXT"


class DnsRecordSchema(Schema):
    """A DNS record the organization must create at its DNS provider."""

    type: DnsRecordType
    name: str
    value: str


class DnsProviderSchema(Schema):
    """The DNS host of a custom domain, with a link to its "add a record" help page."""

    name: str
    help_url: str


class CustomDomainRequestSchema(Schema):
    """Request a custom domain for the organization."""

    hostname: str = Field(..., max_length=253, description="Fully qualified hostname, e.g. events.example.com.")


class CustomDomainSchema(Schema):
    """A custom domain with the DNS instructions the organization must follow."""

    hostname: str
    status: OrganizationDomainStatus
    verification_records: list[DnsRecordSchema]
    cname_target: str | None = None
    verified_at: AwareDatetime | None = None
    errors: list[str] = Field(
        default_factory=list,
        description="Problems Cloudflare reported at the last status sync (hostname verification, certificate).",
    )

    dns_provider: DnsProviderSchema | None = Field(
        default=None,
        description="The DNS host detected from the domain's NS records, with its help page for adding DNS "
        "records. Only set while the domain is pending, and only when the host is a known one.",
    )

    @staticmethod
    def resolve_errors(obj: t.Any) -> list[str]:
        """The problems stored at the last Cloudflare sync."""
        return list(obj.last_errors)

    @staticmethod
    def resolve_cname_target(obj: t.Any) -> str | None:
        """The fallback-origin hostname the domain must CNAME to (``None`` when the platform has not set one)."""
        return settings.CUSTOM_DOMAIN_CNAME_TARGET or None

    @staticmethod
    def resolve_dns_provider(obj: t.Any) -> DnsProviderSchema | None:
        """The detected DNS host, for pending domains only (the others no longer need DNS help)."""
        if obj.status != OrganizationDomain.Status.PENDING:
            return None
        provider = dns_diagnostics.detect_dns_provider(obj.hostname)
        return DnsProviderSchema(name=provider.name, help_url=provider.help_url) if provider else None
