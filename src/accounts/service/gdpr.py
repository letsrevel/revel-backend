"""Privacy utils."""

import io
import json
import zipfile
from typing import Any
from uuid import UUID

from django.core.files.base import ContentFile
from django.core.serializers.json import DjangoJSONEncoder
from django.db.models import ManyToManyRel, ManyToOneRel, OneToOneRel
from django.forms.models import model_to_dict
from django.utils import timezone

from accounts.models import RevelUser, UserDataExport
from questionnaires.models import (
    FreeTextAnswer,
    MultipleChoiceAnswer,
    QuestionnaireSubmission,
)


def generate_user_data_export(user: RevelUser) -> UserDataExport:
    """Generate a data export for a user."""
    export, _ = UserDataExport.objects.get_or_create(user=user)
    export.status = UserDataExport.Status.PROCESSING
    export.save(update_fields=["status"])

    export_data: dict[str, Any] = {}

    user_fields = {
        f.name: getattr(user, f.name) for f in user._meta.fields if f.name not in ["password", "totp_secret_encrypted"]
    }
    export_data["profile"] = user_fields

    # 2. Automatically discover and serialize related objects (depth 1)
    related_objects = [
        f
        for f in user._meta.get_fields()
        if isinstance(f, (OneToOneRel, ManyToOneRel, ManyToManyRel)) and f.related_model
    ]

    for rel in related_objects:
        accessor_name = rel.get_accessor_name()
        if not accessor_name or accessor_name == "data_export":
            continue

        # Handle the questionnaire special case
        if accessor_name == "questionnaire_submissions":
            export_data.update(_serialize_questionnaire_data(user.id))
            continue

        value = getattr(user, accessor_name, None)

        if value is not None:
            if isinstance(rel, OneToOneRel):
                export_data[accessor_name] = model_to_dict(value)
            elif isinstance(rel, (ManyToOneRel, ManyToManyRel)):
                export_data[accessor_name] = [model_to_dict(obj) for obj in value.all()]

    # 3. Create the JSON file in memory
    json_buffer = io.StringIO()
    json.dump(export_data, json_buffer, cls=DjangoJSONEncoder, indent=2)
    json_buffer.seek(0)

    # 4. Create a ZIP archive in memory
    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zip_file:
        zip_file.writestr("revel_user_data.json", json_buffer.read())

    zip_buffer.seek(0)

    # 5. Save the ZIP to the model's FileField
    export.file.save(f"revel_export_{user.id}.zip", ContentFile(zip_buffer.read()), save=False)
    export.status = UserDataExport.Status.READY
    export.completed_at = timezone.now()
    export.save(update_fields=["status", "file", "completed_at"])
    return export


def _serialize_questionnaire_data(user_id: UUID) -> dict[str, list[dict[str, Any]]]:
    """Special case serializer for detailed questionnaire data."""
    submissions = QuestionnaireSubmission.objects.filter(user_id=user_id).select_related("questionnaire", "evaluation")
    data = []
    for sub in submissions:
        sub_data: dict[str, Any] = {
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

        answers: list[dict[str, Any]] = []
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
