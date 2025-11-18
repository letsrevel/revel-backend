"""Common schemas for the API."""

import typing as t

from ninja import Field, ModelSchema, Schema
from pydantic import StringConstraints

from .models import Tag, TagAssignment

UserIdType = t.Annotated[str, Field(..., description="The user ID", max_length=128)]

StrippedString = t.Annotated[str, StringConstraints(strip_whitespace=True)]
OneToSixtyFourString = t.Annotated[str, StringConstraints(min_length=1, max_length=64, strip_whitespace=True)]
OneToOneFiftyString = t.Annotated[str, StringConstraints(min_length=1, max_length=150, strip_whitespace=True)]


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
    terms_and_conditions_html: str
    privacy_policy: str
    privacy_policy_html: str
