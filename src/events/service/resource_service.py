import typing as t

from django.db import transaction
from django.db.models import QuerySet
from ninja.errors import HttpError
from ninja.files import UploadedFile

from events import models, schema
from events.service import update_db_instance


@transaction.atomic
def create_resource(
    organization: models.Organization,
    payload: schema.AdditionalResourceCreateSchema,
    *,
    file: UploadedFile | None = None,
) -> models.AdditionalResource:
    """Creates an AdditionalResource and links it to events and event series.

    Ensures all linked items belong to the same organization.
    Validates that a file is provided when resource_type is FILE.
    """
    # Validate that file is provided for FILE type resources
    if payload.resource_type == models.AdditionalResource.ResourceTypes.FILE and not file:
        raise HttpError(400, "A file must be provided when resource_type is 'file'.")

    m2m_data = _validate_and_prepare_m2m(organization, payload)
    create_data = payload.model_dump(exclude={"event_series_ids", "event_ids"})

    resource = models.AdditionalResource.objects.create(
        organization=organization,
        file=file,
        **create_data,
    )

    if m2m_data["events"]:
        resource.events.set(m2m_data["events"])
    if m2m_data["event_series"]:
        resource.event_series.set(m2m_data["event_series"])

    return resource


@transaction.atomic
def update_resource(
    resource: models.AdditionalResource,
    payload: schema.AdditionalResourceUpdateSchema,
) -> models.AdditionalResource:
    """Updates an AdditionalResource and its M2M relationships.

    Ensures all linked items belong to the correct organization.
    """
    m2m_data = _validate_and_prepare_m2m(resource.organization, payload)
    updated_resource = update_db_instance(
        resource, payload, exclude_unset=True, exclude={"event_series_ids", "event_ids"}
    )

    if "events" in m2m_data:
        updated_resource.events.set(m2m_data["events"])
    if "event_series" in m2m_data:
        updated_resource.event_series.set(m2m_data["event_series"])

    return updated_resource


def _validate_and_prepare_m2m(
    organization: models.Organization,
    payload: schema.AdditionalResourceCreateSchema | schema.AdditionalResourceUpdateSchema,
) -> dict[str, t.Any]:
    """Validates that event and event_series IDs belong to the given organization.

    Returns a dictionary with validated queryset objects for M2M assignment.
    """
    validated_data: dict[str, QuerySet[models.EventSeries] | QuerySet[models.Event]] = {}
    event_ids = payload.event_ids
    series_ids = payload.event_series_ids

    if event_ids is not None:
        events = models.Event.objects.filter(pk__in=event_ids, organization=organization)
        if events.count() != len(event_ids):
            raise HttpError(400, "One or more events do not exist or belong to this organization.")
        validated_data["events"] = events

    if series_ids is not None:
        series = models.EventSeries.objects.filter(pk__in=series_ids, organization=organization)
        if series.count() != len(series_ids):
            raise HttpError(400, "One or more event series do not exist or belong to this organization.")
        validated_data["event_series"] = series

    return validated_data
