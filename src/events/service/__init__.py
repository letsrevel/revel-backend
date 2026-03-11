import typing as t

from django.conf import settings
from django.db import models, transaction
from django.utils.translation import gettext_lazy as _
from ninja.errors import HttpError
from pydantic import BaseModel

from events.models import OrganizationQuestionnaire
from events.schema import OrganizationQuestionnaireUpdateSchema
from questionnaires.models import Questionnaire
from events.service import announcement_service as announcement_service
from events.service import event_questionnaire_service as event_questionnaire_service
from events.service import ticket_file_service as ticket_file_service
from events.service import venue_service as venue_service

T = t.TypeVar("T", bound=models.Model)


def validate_feedback_requires_evaluation(
    questionnaire_type: OrganizationQuestionnaire.QuestionnaireType,
    requires_evaluation: bool,
) -> None:
    """Raise 400 if a feedback questionnaire is configured to require evaluation."""
    if questionnaire_type == OrganizationQuestionnaire.QuestionnaireType.FEEDBACK and requires_evaluation:
        raise HttpError(400, str(_("Feedback questionnaires cannot require evaluation.")))


@transaction.atomic
def update_db_instance(
    instance: T,
    payload: BaseModel | None = None,
    *,
    exclude_unset: bool = True,
    exclude_defaults: bool = False,
    **kwargs: t.Any,
) -> T:
    """Updates a DB instance given a Pydantic payload, safely within a select_for_update lock."""
    instance = instance.__class__.objects.select_for_update().get(pk=instance.pk)  # type: ignore[attr-defined]
    data = payload.model_dump(exclude_unset=exclude_unset, exclude_defaults=exclude_defaults) if payload else {}
    data.update(**kwargs)
    for key, value in data.items():
        setattr(instance, key, value)
    instance.save()
    return instance


@transaction.atomic
def update_organization_questionnaire(
    org_questionnaire: "models.Model",  # OrganizationQuestionnaire
    payload: OrganizationQuestionnaireUpdateSchema,
) -> "models.Model":
    """Update organization questionnaire and its underlying questionnaire.

    Handles updating both OrganizationQuestionnaire wrapper fields and the underlying
    Questionnaire fields, including necessary type conversions. Uses update_db_instance
    for transaction safety and row-level locking.

    Args:
        org_questionnaire: The OrganizationQuestionnaire instance to update
        payload: The update payload containing fields to modify

    Returns:
        The updated OrganizationQuestionnaire instance
    """
    # Validate feature flag: block LLM evaluation modes when the questionnaire has free-text questions
    if not settings.FEATURE_LLM_EVALUATION and payload.evaluation_mode in [
        Questionnaire.QuestionnaireEvaluationMode.AUTOMATIC,
        Questionnaire.QuestionnaireEvaluationMode.HYBRID,
    ]:
        questionnaire = org_questionnaire.questionnaire  # type: ignore[attr-defined]
        has_free_text = questionnaire.freetextquestion_questions.exists()
        if not has_free_text:
            has_free_text = any(
                section.freetextquestion_questions.exists()
                for section in questionnaire.sections.all()
            )
        if has_free_text:
            raise HttpError(400, "LLM evaluation is not available.")

    # Extract questionnaire-specific fields with type conversions
    questionnaire_kwargs = {}
    payload_dict = payload.model_dump(exclude_unset=True)

    # Map fields that belong to Questionnaire
    questionnaire_field_names = {
        "name",
        "min_score",
        "shuffle_questions",
        "shuffle_sections",
        "evaluation_mode",
        "llm_guidelines",
        "max_attempts",
        "can_retake_after",
    }
    for field_name in questionnaire_field_names:
        if field_name in payload_dict:
            questionnaire_kwargs[field_name] = payload_dict[field_name]

    # Update the underlying Questionnaire if there are changes
    if questionnaire_kwargs:
        update_db_instance(
            org_questionnaire.questionnaire,  # type: ignore[attr-defined]
            payload=None,
            exclude_unset=False,
            exclude_defaults=False,
            **questionnaire_kwargs,
        )

    # Extract OrganizationQuestionnaire-specific fields
    org_kwargs = {}
    org_field_names = {"max_submission_age", "questionnaire_type", "members_exempt", "per_event", "requires_evaluation"}
    for field_name in org_field_names:
        if field_name in payload_dict:
            org_kwargs[field_name] = payload_dict[field_name]

    # Validate: feedback questionnaires cannot require evaluation
    effective_type = org_kwargs.get("questionnaire_type", org_questionnaire.questionnaire_type)  # type: ignore[attr-defined]
    effective_requires_eval = org_kwargs.get("requires_evaluation", org_questionnaire.requires_evaluation)  # type: ignore[attr-defined]
    validate_feedback_requires_evaluation(effective_type, effective_requires_eval)

    # Update OrganizationQuestionnaire if there are changes
    if org_kwargs:
        org_questionnaire = update_db_instance(
            org_questionnaire, payload=None, exclude_unset=False, exclude_defaults=False, **org_kwargs
        )

    # Refresh to get updated related questionnaire
    org_questionnaire.refresh_from_db()

    return org_questionnaire
