"""Event series schemas."""

from uuid import UUID

from ninja import Schema
from pydantic import AwareDatetime

from common.schema import OneToOneFiftyString, StrippedString

from .mixins import TaggableSchemaMixin
from .organization import MinimalOrganizationSchema


class MinimalEventSeriesSchema(Schema):
    """Lightweight event series schema for use in event lists - excludes tags and uses minimal organization."""

    id: UUID
    organization: MinimalOrganizationSchema
    name: str
    description: str | None = None
    slug: str
    logo: str | None = None
    cover_art: str | None = None


class EventSeriesInListSchema(TaggableSchemaMixin):
    """Schema for event series list endpoints - includes tags with proper prefetching."""

    id: UUID
    organization: MinimalOrganizationSchema
    name: str
    description: str | None = None
    slug: str
    logo: str | None = None
    cover_art: str | None = None
    updated_at: AwareDatetime | None = None
    created_at: AwareDatetime | None = None


class EventSeriesRetrieveSchema(TaggableSchemaMixin):
    """Full event series schema for detail views - uses minimal organization to prevent cascading queries."""

    id: UUID
    organization: MinimalOrganizationSchema
    name: str
    description: str | None = None
    slug: str
    logo: str | None = None
    cover_art: str | None = None


class EventSeriesEditSchema(Schema):
    name: OneToOneFiftyString
    description: StrippedString | None = None
