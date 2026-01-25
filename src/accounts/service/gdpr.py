"""Privacy utils."""

import io
import typing as t
import zipfile
from uuid import UUID

import orjson
import structlog
from django.contrib.gis.geos import Point
from django.core.files.base import ContentFile
from django.db.models import ManyToManyRel, ManyToOneRel, OneToOneRel
from django.db.models.fields.files import FieldFile
from django.forms.models import model_to_dict
from django.utils import timezone

from accounts.models import RevelUser, UserDataExport
from questionnaires.models import (
    FreeTextAnswer,
    MultipleChoiceAnswer,
    QuestionnaireSubmission,
)

logger = structlog.get_logger(__name__)


def _sanitize_dict_keys(data: t.Any) -> t.Any:
    """Recursively convert all dict keys to strings for orjson compatibility.

    orjson requires all dict keys to be strings. Django's model_to_dict can return
    non-string keys (e.g., FK ids), so we need to stringify them.

    Args:
        data: Data structure to sanitize

    Returns:
        Data with all dict keys as strings
    """
    if isinstance(data, dict):
        return {str(k): _sanitize_dict_keys(v) for k, v in data.items()}
    if isinstance(data, (list, tuple)):
        return [_sanitize_dict_keys(item) for item in data]
    return data


def _default_serializer(obj: t.Any) -> t.Any:
    """Handle Django-specific types for orjson serialization.

    orjson natively handles: str, int, float, bool, None, dict, list, tuple,
    datetime, date, time, UUID, bytes, and more.

    This function only needs to handle Django-specific types that orjson
    doesn't know about.

    Args:
        obj: Object to serialize

    Returns:
        JSON-serializable representation

    Raises:
        TypeError: If object type cannot be serialized (required by orjson)
    """
    # Handle FileField/ImageField - convert to URL
    if isinstance(obj, FieldFile):
        try:
            # Return URL if file exists
            if obj:
                return obj.url
        except Exception:
            # File doesn't exist or storage issue
            logger.debug("gdpr_export_file_url_failed", field_name=getattr(obj, "name", None))
        return "[This field could not be exported]"

    # Handle PostGIS Point - convert to GeoJSON
    if isinstance(obj, Point):
        return {
            "type": "Point",
            "coordinates": [obj.x, obj.y],
        }

    # Fallback: try string representation
    try:
        return str(obj)
    except Exception:
        # If even str() fails, use generic fallback
        # We must raise TypeError to tell orjson we can't handle this,
        # but we want to be permissive for GDPR exports
        logger.warning(
            "gdpr_export_field_serialization_fallback",
            object_type=type(obj).__name__,
        )
        return "[This field could not be exported]"


def _serialize_related_objects(user: RevelUser) -> dict[str, t.Any]:
    """Serialize all related objects for a user.

    Args:
        user: The user whose related objects to serialize

    Returns:
        Dictionary with all related object data
    """
    export_data: dict[str, t.Any] = {}

    # Automatically discover related objects (depth 1)
    related_objects = [
        f
        for f in user._meta.get_fields()
        if isinstance(f, (OneToOneRel, ManyToOneRel, ManyToManyRel)) and f.related_model
    ]

    # Fields to skip from export (internal/privacy-sensitive)
    skip_fields = {
        "data_export",
        "outstandingtoken_set",
        "visible_attendees",
        "visible_to",
        "notifications",
    }

    for rel in related_objects:
        accessor_name = rel.get_accessor_name()
        if not accessor_name or accessor_name in skip_fields:
            continue

        # Handle the questionnaire special case
        if accessor_name == "questionnaire_submissions":
            export_data.update(_serialize_questionnaire_data(user.id))
            continue

        # Handle dietary restrictions - expand food_item details
        if accessor_name == "dietary_restrictions":
            export_data[accessor_name] = _serialize_dietary_restrictions(user)
            continue

        # Handle dietary preferences - expand preference details
        if accessor_name == "dietary_preferences":
            export_data[accessor_name] = _serialize_dietary_preferences(user)
            continue

        value = getattr(user, accessor_name, None)

        if value is not None:
            if isinstance(rel, OneToOneRel):
                export_data[accessor_name] = model_to_dict(value)
            elif isinstance(rel, (ManyToOneRel, ManyToManyRel)):
                export_data[accessor_name] = [model_to_dict(obj) for obj in value.all()]

    return export_data


def generate_user_data_export(user: RevelUser) -> UserDataExport:
    """Generate a data export for a user."""
    logger.info("gdpr_export_started", user_id=str(user.id), email=user.email)

    export: UserDataExport | None = None
    try:
        export, created = UserDataExport.objects.get_or_create(user=user)
        if created:
            logger.info("gdpr_export_created", user_id=str(user.id), export_id=str(export.id))

        export.status = UserDataExport.UserDataExportStatus.PROCESSING
        export.save(update_fields=["status"])

        # 1. Serialize user profile fields
        user_fields = {
            f.name: getattr(user, f.name)
            for f in user._meta.fields
            if f.name not in ["password", "totp_secret_encrypted", "totp_secret"]
        }
        export_data = {"profile": user_fields}

        # 2. Serialize related objects
        export_data.update(_serialize_related_objects(user))

        # 3. Sanitize dict keys (orjson requires string keys) and serialize
        sanitized_data = _sanitize_dict_keys(export_data)
        json_bytes = orjson.dumps(
            sanitized_data,
            default=_default_serializer,
            option=orjson.OPT_INDENT_2,  # Pretty print with 2-space indent
        )

        # 4. Create a ZIP archive in memory
        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zip_file:
            zip_file.writestr("revel_user_data.json", json_bytes)

        zip_buffer.seek(0)

        # 5. Save the ZIP to the model's FileField
        export.file.save(f"revel_export_{user.id}.zip", ContentFile(zip_buffer.read()), save=False)
        export.status = UserDataExport.UserDataExportStatus.READY
        export.completed_at = timezone.now()
        export.save(update_fields=["status", "file", "completed_at"])

        logger.info(
            "gdpr_export_completed",
            user_id=str(user.id),
            export_id=str(export.id),
            file_size_bytes=export.file.size,
            data_categories=list(export_data.keys()),
        )
        return export

    except Exception as e:
        logger.error(
            "gdpr_export_failed",
            user_id=str(user.id),
            error=str(e),
            error_type=type(e).__name__,
            exc_info=True,
        )
        if export:
            export.status = UserDataExport.UserDataExportStatus.FAILED
            export.save(update_fields=["status"])
        raise


def _serialize_dietary_restrictions(user: RevelUser) -> list[dict[str, t.Any]]:
    """Serialize dietary restrictions with expanded food_item details.

    Args:
        user: The user whose restrictions to serialize

    Returns:
        List of restrictions with food_item name included
    """
    restrictions = user.dietary_restrictions.select_related("food_item").all()
    return [
        {
            "food_item_name": restriction.food_item.name,
            "restriction_type": restriction.restriction_type,
            "notes": restriction.notes,
            "is_public": restriction.is_public,
            "created_at": restriction.created_at,
        }
        for restriction in restrictions
    ]


def _serialize_dietary_preferences(user: RevelUser) -> list[dict[str, t.Any]]:
    """Serialize dietary preferences with expanded preference details.

    Args:
        user: The user whose preferences to serialize

    Returns:
        List of preferences with preference name included
    """
    preferences = user.dietary_preferences.select_related("preference").all()
    return [
        {
            "preference_name": pref.preference.name,
            "comment": pref.comment,
            "is_public": pref.is_public,
            "created_at": pref.created_at,
        }
        for pref in preferences
    ]


def _serialize_questionnaire_data(user_id: UUID) -> dict[str, list[dict[str, t.Any]]]:
    """Special case serializer for detailed questionnaire data."""
    submissions = QuestionnaireSubmission.objects.filter(user_id=user_id).select_related("questionnaire", "evaluation")
    data = []
    for sub in submissions:
        sub_data: dict[str, t.Any] = {
            "submission_id": sub.id,
            "questionnaire_name": sub.questionnaire.name,
            "status": sub.status,
            "submitted_at": sub.submitted_at,
            "evaluation": None,
            "answers": [],
        }
        if hasattr(sub, "evaluation") and sub.evaluation:
            sub_data["evaluation"] = {
                "status": sub.evaluation.status,
                "score": sub.evaluation.score,
                "comments": sub.evaluation.comments,
            }

        answers: list[dict[str, t.Any]] = []
        mc_answers = MultipleChoiceAnswer.objects.filter(submission=sub).select_related("question", "option")
        for mc_ans in mc_answers:
            answers.append(
                {
                    "type": "multiple_choice",
                    "question": mc_ans.question.question,
                    "answer": mc_ans.option.option,
                }
            )

        ft_answers = FreeTextAnswer.objects.filter(submission=sub).select_related("question")
        for ft_ans in ft_answers:
            answers.append(
                {
                    "type": "free_text",
                    "question": ft_ans.question.question,
                    "answer": ft_ans.answer,
                }
            )

        sub_data["answers"] = answers
        data.append(sub_data)
    return {"questionnaire_submissions": data}
