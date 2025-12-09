import typing as t

from django.db import models, transaction
from pydantic import BaseModel

from events.schema import OrganizationQuestionnaireUpdateSchema
from events.service import venue_service as venue_service

T = t.TypeVar("T", bound=models.Model)


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
        print(key, value)
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
    org_field_names = {"max_submission_age", "questionnaire_type"}
    for field_name in org_field_names:
        if field_name in payload_dict:
            org_kwargs[field_name] = payload_dict[field_name]

    # Update OrganizationQuestionnaire if there are changes
    if org_kwargs:
        org_questionnaire = update_db_instance(
            org_questionnaire, payload=None, exclude_unset=False, exclude_defaults=False, **org_kwargs
        )

    # Refresh to get updated related questionnaire
    org_questionnaire.refresh_from_db()

    return org_questionnaire
