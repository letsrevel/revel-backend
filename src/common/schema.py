"""Common schemas for the API."""

import typing as t

from ninja import Field, ModelSchema, Schema
from pydantic import AwareDatetime, EmailStr, StringConstraints, field_validator

from common.constants import is_valid_country_code

from .models import SiteSettings, Tag, TagAssignment
from .signing import get_file_url

if t.TYPE_CHECKING:
    from accounts.models import RevelUser


UserIdType = t.Annotated[str, Field(..., description="The user ID", max_length=128)]

StrippedString = t.Annotated[str, StringConstraints(strip_whitespace=True)]
OneToSixtyFourString = t.Annotated[str, StringConstraints(min_length=1, max_length=64, strip_whitespace=True)]
OneToOneFiftyString = t.Annotated[str, StringConstraints(min_length=1, max_length=150, strip_whitespace=True)]

# Reusable annotated type for vat_country_code fields (ISO 3166-1 alpha-2, uppercased, stripped)
VATCountryCode = t.Annotated[str, StringConstraints(strip_whitespace=True, to_upper=True, min_length=2, max_length=2)]


def validate_country_code(v: str | None) -> str | None:
    """Validate a country code is a valid ISO 3166-1 alpha-2 code.

    Accepts empty string (no country set) and None (field not provided).
    Use as a field_validator for vat_country_code fields.
    """
    if v is not None and v and not is_valid_country_code(v):
        raise ValueError(f"Invalid ISO 3166-1 alpha-2 country code: {v}")
    return v


class BillingInfoSchemaMixin(Schema):
    """Shared billing info fields for update schemas (org and user).

    All fields default to empty string. Use ``model_dump(exclude_unset=True)``
    in controllers to distinguish "not sent" from "sent as empty".
    The conflict check between vat_country_code and vat_id prefix
    must be done at the controller level (needs DB state).
    """

    vat_country_code: t.Annotated[str, StringConstraints(strip_whitespace=True, to_upper=True, max_length=2)] = ""
    billing_name: t.Annotated[str, StringConstraints(strip_whitespace=True)] = ""
    billing_address: str = ""
    billing_email: str = ""

    @field_validator("vat_country_code")
    @classmethod
    def validate_vat_country_code(cls, v: str) -> str:
        """Validate vat_country_code is a valid ISO 3166-1 alpha-2 code or empty."""
        return validate_country_code(v) or ""


class EmailSchema(Schema):
    """Common schema for email-only requests."""

    email: EmailStr


class BannerSchema(Schema):
    """Active maintenance banner returned in the /version endpoint."""

    message: str
    severity: SiteSettings.BannerSeverity
    scheduled_at: AwareDatetime | None = None
    ends_at: AwareDatetime | None = None


class VersionResponse(Schema):
    version: str
    demo: bool = False
    banner: BannerSchema | None = None


class ResponseOk(Schema):
    status: t.Literal["ok"] = "ok"


class ResponseMessage(Schema):
    message: str


class ValidationErrorResponse(Schema):
    errors: dict[str, str | list[str]]


class TagSchema(ModelSchema):
    class Meta:
        model = Tag
        fields = ("name", "description", "color", "icon")


class TagAssignmentSchema(ModelSchema):
    tag: TagSchema

    class Meta:
        model = TagAssignment
        fields = ("tag",)


class LegalSchema(Schema):
    terms_and_conditions: str
    privacy_policy: str


class ProfilePictureSchemaMixin(Schema):
    """Mixin for schemas that need to resolve profile picture URLs.

    Schemas using this mixin must have access to a RevelUser object
    with a profile_picture field.
    """

    profile_picture_url: str | None = None
    profile_picture_thumbnail_url: str | None = None
    profile_picture_preview_url: str | None = None

    @staticmethod
    def resolve_profile_picture_url(obj: "RevelUser") -> str | None:
        """Resolve profile picture to signed URL."""
        return get_file_url(obj.profile_picture)

    @staticmethod
    def resolve_profile_picture_thumbnail_url(obj: "RevelUser") -> str | None:
        """Resolve profile picture thumbnail URL (signed)."""
        return get_file_url(obj.profile_picture_thumbnail)

    @staticmethod
    def resolve_profile_picture_preview_url(obj: "RevelUser") -> str | None:
        """Resolve profile picture preview URL (signed)."""
        return get_file_url(obj.profile_picture_preview)
