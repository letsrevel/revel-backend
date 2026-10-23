"""Org-admin custom-domain schemas (white-label, #1151)."""

import enum
import typing as t

from django.conf import settings
from ninja import Schema
from pydantic import AwareDatetime, Field

from events.models import OrganizationDomainStatus


class DnsRecordType(enum.StrEnum):
    """DNS record types an organization may be asked to create."""

    CNAME = "CNAME"
    TXT = "TXT"


class DnsRecordSchema(Schema):
    """A DNS record the organization must create at its DNS provider."""

    type: DnsRecordType
    name: str
    value: str


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

    @staticmethod
    def resolve_errors(obj: t.Any) -> list[str]:
        """The problems stored at the last Cloudflare sync."""
        return list(obj.last_errors)

    @staticmethod
    def resolve_cname_target(obj: t.Any) -> str | None:
        """The fallback-origin hostname the domain must CNAME to (``None`` when the platform has not set one)."""
        return settings.CUSTOM_DOMAIN_CNAME_TARGET or None
