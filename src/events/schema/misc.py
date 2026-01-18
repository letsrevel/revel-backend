"""Miscellaneous schemas - additional resources, etc."""

from uuid import UUID

from ninja import ModelSchema, Schema
from pydantic import Field, model_validator

from common.signing import get_file_url
from events.models import AdditionalResource, ResourceVisibility


class AdditionalResourceSchema(ModelSchema):
    """Schema for AdditionalResource with signed file URLs.

    File URLs are automatically signed for protected paths (upload_to="file").
    The `file_url` field contains the signed URL; the raw `file` path is not exposed.
    """

    event_ids: list[UUID] = Field(default_factory=list)
    event_series_ids: list[UUID] = Field(default_factory=list)
    file_url: str | None = Field(default=None)

    @staticmethod
    def resolve_file_url(obj: AdditionalResource) -> str | None:
        """Return signed URL for the file if it's a protected path."""
        return get_file_url(obj.file)

    @staticmethod
    def resolve_event_ids(obj: AdditionalResource) -> list[UUID]:
        """Return list of event UUIDs this resource is linked to.

        Uses values_list to fetch only IDs, avoiding loading full Event objects.
        """
        return list(obj.events.values_list("pk", flat=True))

    @staticmethod
    def resolve_event_series_ids(obj: AdditionalResource) -> list[UUID]:
        """Return list of event series UUIDs this resource is linked to.

        Uses values_list to fetch only IDs, avoiding loading full EventSeries objects.
        """
        return list(obj.event_series.values_list("pk", flat=True))

    class Meta:
        model = AdditionalResource
        fields = [
            "id",
            "resource_type",
            "name",
            "description",
            "link",
            "text",
            "visibility",
            "display_on_organization_page",
        ]


class AdditionalResourceCreateSchema(Schema):
    name: str | None = None
    description: str | None = None
    resource_type: AdditionalResource.ResourceTypes
    visibility: ResourceVisibility = ResourceVisibility.MEMBERS_ONLY
    display_on_organization_page: bool = True
    link: str | None = None
    text: str | None = None
    event_series_ids: list[UUID] = Field(default_factory=list)
    event_ids: list[UUID] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_resource_content(self) -> "AdditionalResourceCreateSchema":
        """Ensure content fields match the resource_type.

        For FILE type: link and text must be None (file is passed separately as multipart).
        For LINK or TEXT type: exactly one of link or text must be provided and match resource_type.
        """
        content_fields = {"link": self.link, "text": self.text}
        provided_fields = [field for field, value in content_fields.items() if value]

        if self.resource_type == AdditionalResource.ResourceTypes.FILE:
            # For FILE type, link and text must not be provided (file comes separately)
            if provided_fields:
                raise ValueError(
                    f"When resource_type is 'file', 'link' and 'text' must not be provided. "
                    f"Found: {', '.join(provided_fields)}"
                )
        else:
            # For LINK or TEXT type, exactly one must be provided and match the type
            if len(provided_fields) != 1:
                raise ValueError(
                    f"For resource_type '{self.resource_type}', exactly one of 'link' or 'text' must be provided. "
                    f"Found: {len(provided_fields)}"
                )

            if provided_fields[0] != self.resource_type:
                raise ValueError(
                    f"The provided content field '{provided_fields[0]}' does not match "
                    f"the resource_type '{self.resource_type}'."
                )

        return self


class AdditionalResourceUpdateSchema(Schema):
    name: str | None = None
    description: str | None = None
    visibility: ResourceVisibility | None = None
    display_on_organization_page: bool | None = None
    link: str | None = None
    text: str | None = None
    event_series_ids: list[UUID] | None = None
    event_ids: list[UUID] | None = None
