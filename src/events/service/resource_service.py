import hashlib
import typing as t
import uuid

import magic
from django.db import transaction
from django.db.models import QuerySet
from django.utils.translation import gettext_lazy as _
from ninja.errors import HttpError
from ninja.files import UploadedFile

from accounts.models import RevelUser
from common.file_types import extension_for_mime
from common.service.upload_service import create_file_audit_and_scan
from common.utils import strip_exif
from events import models, schema
from events.service import update_db_instance
from questionnaires.schema import ALLOWED_QUESTIONNAIRE_MIME_TYPES, IMAGE_MIME_TYPES

# Maximum size of a resource file upload (25MB).
MAX_RESOURCE_FILE_SIZE = 25 * 1024 * 1024


@transaction.atomic
def create_resource(
    organization: models.Organization,
    payload: schema.AdditionalResourceCreateSchema,
    *,
    file: UploadedFile | None = None,
    uploader: RevelUser,
) -> models.AdditionalResource:
    """Creates an AdditionalResource and links it to events and event series.

    Ensures all linked items belong to the same organization.
    Validates that a file is provided when resource_type is FILE.
    An uploaded file is size-capped, type-checked from its content against the
    questionnaire allowlist, stripped of image metadata, stored under a
    server-chosen name, and registered for the malware scan.

    Args:
        organization: The owning organization.
        payload: The resource data.
        file: The uploaded file, for FILE resources.
        uploader: The acting user, recorded in the upload audit.

    Raises:
        HttpError: 400 if a FILE resource has no file or links do not belong to the
            organization; 422 if the file is too large or of a disallowed type.
    """
    # Validate that file is provided for FILE type resources
    if payload.resource_type == models.AdditionalResource.ResourceTypes.FILE and not file:
        raise HttpError(400, str(_("A file must be provided when resource_type is 'file'.")))

    m2m_data = _validate_and_prepare_m2m(organization, payload)
    create_data = payload.model_dump(exclude={"event_series_ids", "event_ids"})

    file_hash = ""
    if file:
        file, file_hash = _prepare_resource_file(file)

    resource = models.AdditionalResource.objects.create(
        organization=organization,
        file=file,
        **create_data,
    )

    # Refresh from DB to ensure file path is fully populated after storage
    # (needed for signed URL generation in the response)
    if file:
        resource.refresh_from_db()
        create_file_audit_and_scan(
            app="events",
            model="additionalresource",
            instance_pk=resource.pk,
            field="file",
            file_hash=file_hash,
            uploader_email=uploader.email,
        )

    if m2m_data["events"]:
        resource.events.set(m2m_data["events"])
    if m2m_data["event_series"]:
        resource.event_series.set(m2m_data["event_series"])

    return resource


def _prepare_resource_file(file: UploadedFile) -> tuple[UploadedFile, str]:
    """Validate an uploaded resource file and give it a server-chosen name.

    The type is detected from the content (the client header and name are not trusted)
    and the stored extension comes from that detected type.

    Returns:
        The processed file and the SHA-256 of its stored content.
    """
    if file.size and file.size > MAX_RESOURCE_FILE_SIZE:
        raise HttpError(
            422, str(_("File exceeds maximum upload size of {}MB.").format(MAX_RESOURCE_FILE_SIZE // (1024 * 1024)))
        )

    detected_mime_type = magic.from_buffer(file.read(), mime=True)
    file.seek(0)
    if detected_mime_type not in ALLOWED_QUESTIONNAIRE_MIME_TYPES:
        raise HttpError(
            422,
            str(
                _(
                    "File type '{}' is not allowed. Allowed types: documents, images, audio, video, and archives."
                ).format(detected_mime_type)
            ),
        )

    if detected_mime_type in IMAGE_MIME_TYPES:
        file = strip_exif(file)  # type: ignore[assignment]
    content = file.read()
    file.seek(0)
    file.name = f"{uuid.uuid4()}{extension_for_mime(detected_mime_type)}"
    return file, hashlib.sha256(content).hexdigest()


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
            raise HttpError(400, str(_("One or more events do not exist or belong to this organization.")))
        validated_data["events"] = events

    if series_ids is not None:
        series = models.EventSeries.objects.filter(pk__in=series_ids, organization=organization)
        if series.count() != len(series_ids):
            raise HttpError(400, str(_("One or more event series do not exist or belong to this organization.")))
        validated_data["event_series"] = series

    return validated_data
