"""Common schemas for the API."""

import typing as t

from ninja import Field, ModelSchema, Schema
from pydantic import EmailStr, StringConstraints

from .models import Tag, TagAssignment
from .signing import get_file_url

if t.TYPE_CHECKING:
    from accounts.models import RevelUser


UserIdType = t.Annotated[str, Field(..., description="The user ID", max_length=128)]

StrippedString = t.Annotated[str, StringConstraints(strip_whitespace=True)]
OneToSixtyFourString = t.Annotated[str, StringConstraints(min_length=1, max_length=64, strip_whitespace=True)]
OneToOneFiftyString = t.Annotated[str, StringConstraints(min_length=1, max_length=150, strip_whitespace=True)]


class EmailSchema(Schema):
    """Common schema for email-only requests."""

    email: EmailStr


class VersionResponse(Schema):
    version: str
    demo: bool = False


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
