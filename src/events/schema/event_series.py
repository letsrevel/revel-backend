"""Event series schemas."""

import typing as t
from uuid import UUID

from ninja import Schema
from pydantic import AwareDatetime

from common.schema import OneToOneFiftyString, StrippedString

from .mixins import LogoCoverArtThumbnailMixin, TaggableSchemaMixin
from .organization import MinimalOrganizationSchema


class MinimalEventSeriesSchema(LogoCoverArtThumbnailMixin):
    """Lightweight event series schema for use in event lists - excludes tags and uses minimal organization."""

    id: UUID
    organization: MinimalOrganizationSchema
    name: str
    description: str | None = None
    slug: str
    logo: str | None = None
    cover_art: str | None = None


class EventSeriesInListSchema(TaggableSchemaMixin, LogoCoverArtThumbnailMixin):
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
    is_recurring: bool = False

    @staticmethod
    def resolve_is_recurring(obj: t.Any) -> bool:  # noqa: ANN401
        """Return True if the series is driven by a recurrence rule (vs. a manual grouping container).

        Reads the ``is_recurring`` annotation produced by ``EventSeriesQuerySet.with_is_recurring()``
        (an ``EXISTS`` subquery against ``RecurrenceRule``), so list endpoints stay N+1-free.
        Falls back to the FK check for object-only access paths (e.g. direct ``.get(pk=...)``).
        """
        annotated = getattr(obj, "is_recurring", None)
        if annotated is not None:
            return bool(annotated)
        return bool(getattr(obj, "recurrence_rule_id", None))


class EventSeriesRetrieveSchema(TaggableSchemaMixin, LogoCoverArtThumbnailMixin):
    """Full event series schema for detail views - uses minimal organization to prevent cascading queries."""

    id: UUID
    organization: MinimalOrganizationSchema
    name: str
    description: str | None = None
    slug: str
    logo: str | None = None
    cover_art: str | None = None
    is_active: bool = True
    auto_publish: bool = False
    is_recurring: bool = False

    @staticmethod
    def resolve_is_recurring(obj: t.Any) -> bool:  # noqa: ANN401
        """Return True if the series is driven by a recurrence rule (vs. a manual grouping container).

        Reads the ``is_recurring`` annotation produced by ``EventSeriesQuerySet.with_is_recurring()``
        (an ``EXISTS`` subquery against ``RecurrenceRule``), so list endpoints stay N+1-free.
        Falls back to the FK check for object-only access paths (e.g. direct ``.get(pk=...)``).
        """
        annotated = getattr(obj, "is_recurring", None)
        if annotated is not None:
            return bool(annotated)
        return bool(getattr(obj, "recurrence_rule_id", None))


class EventSeriesEditSchema(Schema):
    name: OneToOneFiftyString
    description: StrippedString | None = None
