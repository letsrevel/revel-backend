"""Common schemas for the API."""

import typing as t

from ninja import Field, ModelSchema, Schema
from pydantic import EmailStr, StringConstraints

from .models import Tag, TagAssignment
from .signing import get_file_url

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


class SignedFileSchemaMixin:
    """Mixin for schemas with signed file URL fields.

    This mixin provides:
    1. Validation that URL fields referenced in `signed_file_fields` are declared
    2. Auto-generated resolvers for non-Pydantic use cases

    Usage:
        class MySchema(SignedFileSchemaMixin, ModelSchema):
            signed_file_fields: t.ClassVar[dict[str, str]] = {
                "file_url": "file",           # file_url field resolves from obj.file
            }

            file_url: str | None = None

            # IMPORTANT: For Django Ninja ModelSchema, you MUST define the resolver
            # explicitly due to Pydantic metaclass processing order:
            @staticmethod
            def resolve_file_url(obj: MyModel) -> str | None:
                return get_file_url(obj.file)

            class Meta:
                model = MyModel
                fields = ["id", "name"]

    The resolver should:
    - Return None if the file field is empty
    - Return a signed URL if the path is protected
    - Return a direct URL if the path is public

    Note:
        For Django Ninja/Pydantic ModelSchema classes, you must define resolvers
        explicitly using `@staticmethod def resolve_{field}`. The mixin's
        auto-generated resolvers won't be picked up due to Pydantic's metaclass
        processing order (class setup happens before __init_subclass__).

        The mixin still validates that declared fields exist and creates resolvers
        that work when called directly (useful for testing or non-Pydantic contexts).
    """

    signed_file_fields: t.ClassVar[dict[str, str]] = {}

    def __init_subclass__(cls, **kwargs: t.Any) -> None:
        super().__init_subclass__(**kwargs)

        # Get the signed_file_fields from this class (not inherited)
        signed_fields = cls.__dict__.get("signed_file_fields", {})

        # Collect all annotations from the class hierarchy
        all_annotations: dict[str, t.Any] = {}
        for base in reversed(cls.__mro__):
            all_annotations.update(getattr(base, "__annotations__", {}))

        for url_field, model_field in signed_fields.items():
            # Validate that the URL field is declared in the schema
            if url_field not in all_annotations:
                raise TypeError(
                    f"{cls.__name__}: signed_file_fields references '{url_field}' "
                    f"but this field is not declared in the schema annotations"
                )

            resolver_name = f"resolve_{url_field}"

            # Skip if resolver already defined (allow manual override)
            if resolver_name in cls.__dict__:
                continue

            # Create resolver function with closure over model_field
            def make_resolver(mf: str) -> t.Any:
                def resolver(obj: t.Any) -> str | None:
                    file_value = getattr(obj, mf, None)
                    return get_file_url(file_value)

                return staticmethod(resolver)

            setattr(cls, resolver_name, make_resolver(model_field))
