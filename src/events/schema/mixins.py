"""Base mixins and shared utilities for event schemas."""

import re
import typing as t

from ninja import Schema
from pydantic import AnyUrl, Field, HttpUrl, field_validator

from common.schema import StrippedString
from events import models
from geo.models import City
from geo.schema import CitySchema


def ensure_url(value: str) -> str:
    """Mock function for now."""
    if not value.startswith("http"):
        return "http://localhost:8000" + value
    return value


class CityBaseMixin(Schema):
    city_id: int | None = None

    @field_validator("city_id", mode="after")
    @classmethod
    def validate_city_exists(cls, v: int | None) -> int | None:
        """Validate that city exists."""
        if v is not None and not City.objects.filter(pk=v).exists():
            raise ValueError(f"City with ID {v} does not exist.")
        return v


class CityEditMixin(CityBaseMixin):
    address: StrippedString | None = None
    location_maps_url: HttpUrl | None = None
    location_maps_embed: StrippedString | None = None

    @field_validator("location_maps_embed", mode="after")
    @classmethod
    def extract_src_from_iframe(cls, v: str | None) -> str | None:
        """Extract src URL from iframe HTML, or pass through if already a URL.

        Users paste the full iframe from Google Maps share dialog.
        We extract and store just the src URL for cleaner data storage
        and frontend flexibility.

        Also accepts already-extracted URLs (for re-saving existing data).
        """
        if not v:
            return None
        # Already a URL (re-saving existing data) - pass through
        if v.startswith(("http://", "https://")):
            return v
        # Must be an iframe
        if not v.lower().startswith("<iframe"):
            raise ValueError("Must be an iframe element (paste the embed code from Google Maps)")
        match = re.search(r'src=["\']([^"\']+)["\']', v)
        if not match:
            raise ValueError("Could not extract src URL from iframe")
        return match.group(1)


class CityRetrieveMixin(Schema):
    city: CitySchema | None = None
    address: str | None = None
    location_maps_url: str | None = None
    location_maps_embed: str | None = None


class TaggableSchemaMixin(Schema):
    tags: list[str] = Field(default_factory=list)

    @staticmethod
    def resolve_tags(obj: models.Event) -> list[str]:
        """Flattify tags."""
        if hasattr(obj, "prefetched_tagassignments"):
            return [ta.tag.name for ta in obj.prefetched_tagassignments]
        return [ta.tag.name for ta in obj.tags.all()]


# Social media URL field names
_SOCIAL_MEDIA_FIELDS = ("instagram_url", "facebook_url", "bluesky_url", "telegram_url")

# Social media platform URL patterns for validation
_SOCIAL_MEDIA_PATTERNS: dict[str, tuple[str, ...]] = {
    "instagram_url": ("instagram.com", "www.instagram.com"),
    "facebook_url": ("facebook.com", "www.facebook.com", "fb.com", "www.fb.com"),
    "bluesky_url": ("bsky.app", "bsky.social"),
    "telegram_url": ("t.me", "telegram.me", "telegram.dog"),
}


def _validate_social_media_url(url: str, field_name: str) -> None:
    """Validate that a URL matches the expected social media platform.

    Args:
        url: The URL to validate.
        field_name: The field name to look up allowed domains.

    Raises:
        ValueError: If the URL doesn't match the expected platform.
    """
    from urllib.parse import urlparse

    allowed_domains = _SOCIAL_MEDIA_PATTERNS.get(field_name, ())
    if not allowed_domains:
        return

    parsed = urlparse(url)
    hostname = parsed.hostname or ""

    if hostname not in allowed_domains:
        platform_name = field_name.replace("_url", "").replace("_", " ").title()
        raise ValueError(
            f"URL must be a valid {platform_name} link. Got: url={url} | {hostname!r} not in {allowed_domains}"
        )


class SocialMediaSchemaRetrieveMixin(Schema):
    """Mixin for reading social media URL fields. No validation needed."""

    instagram_url: str | None = None
    facebook_url: str | None = None
    bluesky_url: str | None = None
    telegram_url: str | None = None


class SocialMediaSchemaEditMixin(Schema):
    """Mixin for editing social media URL fields with platform validation.

    - Automatically prepends https:// if no scheme is provided
    - Validates that each URL matches its expected platform domain
    """

    instagram_url: AnyUrl | None = None
    facebook_url: AnyUrl | None = None
    bluesky_url: AnyUrl | None = None
    telegram_url: AnyUrl | None = None

    @field_validator(*_SOCIAL_MEDIA_FIELDS, mode="before")
    @classmethod
    def validate_social_media_urls(cls, v: t.Any, info: t.Any) -> str | None:
        """Prepend https:// if needed and validate platform domain."""
        if not v or not isinstance(v, str):
            return None

        # Prepend https:// if no scheme provided
        url: str = v if v.startswith(("http://", "https://")) else f"https://{v}"

        # Validate platform domain
        _validate_social_media_url(url, info.field_name)
        return url
