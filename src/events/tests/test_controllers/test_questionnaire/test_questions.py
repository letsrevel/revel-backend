"""Tests for questionnaire question CRUD operations.

Tests for multiple choice questions, multiple choice options, and free text questions.
"""

from decimal import Decimal

import pytest
from django.test import Client
from django.urls import reverse

from events.models import Organization, OrganizationQuestionnaire
from questionnaires.models import (
    FreeTextQuestion,
    MultipleChoiceOption,
    MultipleChoiceQuestion,
    Questionnaire,
)
from questionnaires.schema import (
    FreeTextQuestionCreateSchema,
    FreeTextQuestionUpdateSchema,
    MultipleChoiceOptionCreateSchema,
    MultipleChoiceOptionUpdateSchema,
    MultipleChoiceQuestionCreateSchema,
    MultipleChoiceQuestionUpdateSchema,
)

pytestmark = pytest.mark.django_db


# --- Create MC question tests ---


def test_create_mc_question_success(organization: Organization, organization_owner_client: Client) -> None:
    """Test that a multiple choice question can be created."""
    questionnaire = Questionnaire.objects.create(name="Test Questionnaire")
    org_questionnaire = OrganizationQuestionnaire.objects.create(organization=organization, questionnaire=questionnaire)

    payload = MultipleChoiceQuestionCreateSchema(
        question="What is your favorite color?",
        is_mandatory=True,
        order=1,
        allow_multiple_answers=False,
        options=[
            MultipleChoiceOptionCreateSchema(option="Red", is_correct=True, order=1),
            MultipleChoiceOptionCreateSchema(option="Blue", is_correct=False, order=2),
        ],
    )

    url = reverse("api:create_mc_question", kwargs={"org_questionnaire_id": org_questionnaire.id})
    response = organization_owner_client.post(url, data=payload.model_dump_json(), content_type="application/json")

    assert response.status_code == 200
    data = response.json()
    assert data["question"] == "What is your favorite color?"
    assert data["is_mandatory"] is True
    assert data["allow_multiple_answers"] is False
    assert len(data["options"]) == 2

    # Verify question was created
    question = MultipleChoiceQuestion.objects.get(questionnaire=questionnaire)
    assert question.question == "What is your favorite color?"
    assert question.options.count() == 2


def test_create_mc_question_permission_denied(organization: Organization, nonmember_client: Client) -> None:
    """Test that non-members cannot create multiple choice questions."""
    questionnaire = Questionnaire.objects.create(name="Test Questionnaire")
    org_questionnaire = OrganizationQuestionnaire.objects.create(organization=organization, questionnaire=questionnaire)

    payload = MultipleChoiceQuestionCreateSchema(
        question="What is your favorite color?",
        options=[MultipleChoiceOptionCreateSchema(option="Red", is_correct=True, order=1)],
    )

    url = reverse("api:create_mc_question", kwargs={"org_questionnaire_id": org_questionnaire.id})
    response = nonmember_client.post(url, data=payload.model_dump_json(), content_type="application/json")

    assert response.status_code == 403


# --- Update MC question tests ---


def test_update_mc_question_success(organization: Organization, organization_owner_client: Client) -> None:
    """Test that a multiple choice question can be updated."""
    questionnaire = Questionnaire.objects.create(name="Test Questionnaire")
    org_questionnaire = OrganizationQuestionnaire.objects.create(organization=organization, questionnaire=questionnaire)
    question = MultipleChoiceQuestion.objects.create(
        questionnaire=questionnaire, question="Original Question", order=1, allow_multiple_answers=False
    )

    payload = MultipleChoiceQuestionUpdateSchema(
        question="Updated Question",
        is_mandatory=True,
        order=2,
        allow_multiple_answers=True,
        options=[
            MultipleChoiceOptionCreateSchema(option="Option A", is_correct=True, order=1),
            MultipleChoiceOptionCreateSchema(option="Option B", is_correct=False, order=2),
        ],
    )

    url = reverse(
        "api:update_mc_question", kwargs={"org_questionnaire_id": org_questionnaire.id, "question_id": question.id}
    )
    response = organization_owner_client.put(url, data=payload.model_dump_json(), content_type="application/json")

    assert response.status_code == 200
    data = response.json()
    assert data["question"] == "Updated Question"
    assert data["is_mandatory"] is True
    assert data["allow_multiple_answers"] is True
    assert len(data["options"]) == 2

    # Verify question was updated
    question.refresh_from_db()
    assert question.question == "Updated Question"
    assert question.allow_multiple_answers is True


def test_update_mc_question_not_found(organization: Organization, organization_owner_client: Client) -> None:
    """Test that updating a non-existent question returns 404."""
    from uuid import uuid4

    questionnaire = Questionnaire.objects.create(name="Test Questionnaire")
    org_questionnaire = OrganizationQuestionnaire.objects.create(organization=organization, questionnaire=questionnaire)

    payload = MultipleChoiceQuestionUpdateSchema(question="Updated Question", options=[])

    url = reverse(
        "api:update_mc_question", kwargs={"org_questionnaire_id": org_questionnaire.id, "question_id": uuid4()}
    )
    response = organization_owner_client.put(url, data=payload.model_dump_json(), content_type="application/json")

    assert response.status_code == 404


# --- Delete MC question tests ---


def test_delete_mc_question_success(organization: Organization, organization_owner_client: Client) -> None:
    """Test that a multiple choice question can be deleted."""
    questionnaire = Questionnaire.objects.create(name="Test Questionnaire")
    org_questionnaire = OrganizationQuestionnaire.objects.create(organization=organization, questionnaire=questionnaire)
    question = MultipleChoiceQuestion.objects.create(questionnaire=questionnaire, question="Test Question", order=1)

    url = reverse(
        "api:delete_mc_question", kwargs={"org_questionnaire_id": org_questionnaire.id, "question_id": question.id}
    )
    response = organization_owner_client.delete(url)

    assert response.status_code == 204
    assert not MultipleChoiceQuestion.objects.filter(id=question.id).exists()


def test_delete_mc_question_not_found(organization: Organization, organization_owner_client: Client) -> None:
    """Test that deleting a non-existent MC question returns 404."""
    from uuid import uuid4

    questionnaire = Questionnaire.objects.create(name="Test Questionnaire")
    org_questionnaire = OrganizationQuestionnaire.objects.create(organization=organization, questionnaire=questionnaire)

    url = reverse(
        "api:delete_mc_question", kwargs={"org_questionnaire_id": org_questionnaire.id, "question_id": uuid4()}
    )
    response = organization_owner_client.delete(url)

    assert response.status_code == 404


def test_delete_mc_question_permission_denied(organization: Organization, nonmember_client: Client) -> None:
    """Test that non-members cannot delete MC questions."""
    questionnaire = Questionnaire.objects.create(name="Test Questionnaire")
    org_questionnaire = OrganizationQuestionnaire.objects.create(organization=organization, questionnaire=questionnaire)
    question = MultipleChoiceQuestion.objects.create(questionnaire=questionnaire, question="Test Question", order=1)

    url = reverse(
        "api:delete_mc_question", kwargs={"org_questionnaire_id": org_questionnaire.id, "question_id": question.id}
    )
    response = nonmember_client.delete(url)

    assert response.status_code == 404


# --- Create MC option tests ---


def test_create_mc_option_success(organization: Organization, organization_owner_client: Client) -> None:
    """Test that a multiple choice option can be created."""
    questionnaire = Questionnaire.objects.create(name="Test Questionnaire")
    org_questionnaire = OrganizationQuestionnaire.objects.create(organization=organization, questionnaire=questionnaire)
    question = MultipleChoiceQuestion.objects.create(questionnaire=questionnaire, question="Test Question", order=1)

    payload = MultipleChoiceOptionCreateSchema(option="New Option", is_correct=True, order=1)

    url = reverse(
        "api:create_mc_option", kwargs={"org_questionnaire_id": org_questionnaire.id, "question_id": question.id}
    )
    response = organization_owner_client.post(url, data=payload.model_dump_json(), content_type="application/json")

    assert response.status_code == 200
    data = response.json()
    assert data["option"] == "New Option"
    assert data["is_correct"] is True
    assert data["order"] == 1

    # Verify option was created
    option = MultipleChoiceOption.objects.get(question=question, option="New Option")
    assert option.is_correct is True


def test_create_mc_option_permission_denied(organization: Organization, nonmember_client: Client) -> None:
    """Test that non-members cannot create multiple choice options."""
    questionnaire = Questionnaire.objects.create(name="Test Questionnaire")
    org_questionnaire = OrganizationQuestionnaire.objects.create(organization=organization, questionnaire=questionnaire)
    question = MultipleChoiceQuestion.objects.create(questionnaire=questionnaire, question="Test Question", order=1)

    payload = MultipleChoiceOptionCreateSchema(option="New Option", is_correct=True, order=1)

    url = reverse(
        "api:create_mc_option", kwargs={"org_questionnaire_id": org_questionnaire.id, "question_id": question.id}
    )
    response = nonmember_client.post(url, data=payload.model_dump_json(), content_type="application/json")

    assert response.status_code == 403


# --- Update MC option tests ---


def test_update_mc_option_success(organization: Organization, organization_owner_client: Client) -> None:
    """Test that a multiple choice option can be updated."""
    questionnaire = Questionnaire.objects.create(name="Test Questionnaire")
    org_questionnaire = OrganizationQuestionnaire.objects.create(organization=organization, questionnaire=questionnaire)
    question = MultipleChoiceQuestion.objects.create(questionnaire=questionnaire, question="Test Question", order=1)
    option = MultipleChoiceOption.objects.create(question=question, option="Original Option", is_correct=False, order=1)

    payload = MultipleChoiceOptionUpdateSchema(option="Updated Option", is_correct=True, order=2)

    url = reverse("api:update_mc_option", kwargs={"org_questionnaire_id": org_questionnaire.id, "option_id": option.id})
    response = organization_owner_client.put(url, data=payload.model_dump_json(), content_type="application/json")

    assert response.status_code == 200
    data = response.json()
    assert data["option"] == "Updated Option"
    assert data["is_correct"] is True
    assert data["order"] == 2

    # Verify option was updated
    option.refresh_from_db()
    assert option.option == "Updated Option"
    assert option.is_correct is True


def test_update_mc_option_not_found(organization: Organization, organization_owner_client: Client) -> None:
    """Test that updating a non-existent option returns 404."""
    from uuid import uuid4

    questionnaire = Questionnaire.objects.create(name="Test Questionnaire")
    org_questionnaire = OrganizationQuestionnaire.objects.create(organization=organization, questionnaire=questionnaire)

    payload = MultipleChoiceOptionUpdateSchema(option="Updated Option", is_correct=True, order=1)

    url = reverse("api:update_mc_option", kwargs={"org_questionnaire_id": org_questionnaire.id, "option_id": uuid4()})
    response = organization_owner_client.put(url, data=payload.model_dump_json(), content_type="application/json")

    assert response.status_code == 404


# --- Delete MC option tests ---


def test_delete_mc_option_success(organization: Organization, organization_owner_client: Client) -> None:
    """Test that a multiple choice option can be deleted."""
    questionnaire = Questionnaire.objects.create(name="Test Questionnaire")
    org_questionnaire = OrganizationQuestionnaire.objects.create(organization=organization, questionnaire=questionnaire)
    question = MultipleChoiceQuestion.objects.create(questionnaire=questionnaire, question="Test Question", order=1)
    option = MultipleChoiceOption.objects.create(question=question, option="Test Option", is_correct=True, order=1)

    url = reverse("api:delete_mc_option", kwargs={"org_questionnaire_id": org_questionnaire.id, "option_id": option.id})
    response = organization_owner_client.delete(url)

    assert response.status_code == 204
    assert not MultipleChoiceOption.objects.filter(id=option.id).exists()


def test_delete_mc_option_not_found(organization: Organization, organization_owner_client: Client) -> None:
    """Test that deleting a non-existent MC option returns 404."""
    from uuid import uuid4

    questionnaire = Questionnaire.objects.create(name="Test Questionnaire")
    org_questionnaire = OrganizationQuestionnaire.objects.create(organization=organization, questionnaire=questionnaire)

    url = reverse("api:delete_mc_option", kwargs={"org_questionnaire_id": org_questionnaire.id, "option_id": uuid4()})
    response = organization_owner_client.delete(url)

    assert response.status_code == 404


def test_delete_mc_option_permission_denied(organization: Organization, nonmember_client: Client) -> None:
    """Test that non-members cannot delete MC options."""
    questionnaire = Questionnaire.objects.create(name="Test Questionnaire")
    org_questionnaire = OrganizationQuestionnaire.objects.create(organization=organization, questionnaire=questionnaire)
    question = MultipleChoiceQuestion.objects.create(questionnaire=questionnaire, question="Test Question", order=1)
    option = MultipleChoiceOption.objects.create(question=question, option="Test Option", is_correct=True, order=1)

    url = reverse("api:delete_mc_option", kwargs={"org_questionnaire_id": org_questionnaire.id, "option_id": option.id})
    response = nonmember_client.delete(url)

    assert response.status_code == 404


# --- Create FT question tests ---


def test_create_ft_question_success(organization: Organization, organization_owner_client: Client) -> None:
    """Test that a free text question can be created."""
    questionnaire = Questionnaire.objects.create(name="Test Questionnaire")
    org_questionnaire = OrganizationQuestionnaire.objects.create(organization=organization, questionnaire=questionnaire)

    payload = FreeTextQuestionCreateSchema(
        question="Explain your reasoning.",
        is_mandatory=True,
        order=1,
        positive_weight=Decimal("2.0"),
        negative_weight=Decimal("1.0"),
        is_fatal=True,
        llm_guidelines="Focus on clarity and logic.",
    )

    url = reverse("api:create_ft_question", kwargs={"org_questionnaire_id": org_questionnaire.id})
    response = organization_owner_client.post(url, data=payload.model_dump_json(), content_type="application/json")

    assert response.status_code == 200
    data = response.json()
    assert data["question"] == "Explain your reasoning."
    assert data["is_mandatory"] is True
    assert data["order"] == 1
    assert Decimal(data["positive_weight"]) == Decimal("2.00")
    assert Decimal(data["negative_weight"]) == Decimal("1.00")
    assert data["is_fatal"] is True

    # Verify question was created
    question = FreeTextQuestion.objects.get(questionnaire=questionnaire)
    assert question.question == "Explain your reasoning."
    assert question.is_fatal is True


def test_create_ft_question_permission_denied(organization: Organization, nonmember_client: Client) -> None:
    """Test that non-members cannot create free text questions."""
    questionnaire = Questionnaire.objects.create(name="Test Questionnaire")
    org_questionnaire = OrganizationQuestionnaire.objects.create(organization=organization, questionnaire=questionnaire)

    payload = FreeTextQuestionCreateSchema(question="Explain your reasoning.")

    url = reverse("api:create_ft_question", kwargs={"org_questionnaire_id": org_questionnaire.id})
    response = nonmember_client.post(url, data=payload.model_dump_json(), content_type="application/json")

    assert response.status_code == 403


# --- Update FT question tests ---


def test_update_ft_question_success(organization: Organization, organization_owner_client: Client) -> None:
    """Test that a free text question can be updated."""
    questionnaire = Questionnaire.objects.create(name="Test Questionnaire")
    org_questionnaire = OrganizationQuestionnaire.objects.create(organization=organization, questionnaire=questionnaire)
    question = FreeTextQuestion.objects.create(
        questionnaire=questionnaire,
        question="Original Question",
        order=1,
        positive_weight=Decimal("1.0"),
        is_fatal=False,
    )

    payload = FreeTextQuestionUpdateSchema(
        question="Updated Question",
        is_mandatory=True,
        order=2,
        positive_weight=Decimal("3.0"),
        negative_weight=Decimal("2.0"),
        is_fatal=True,
        llm_guidelines="Updated guidelines.",
    )

    url = reverse(
        "api:update_ft_question", kwargs={"org_questionnaire_id": org_questionnaire.id, "question_id": question.id}
    )
    response = organization_owner_client.put(url, data=payload.model_dump_json(), content_type="application/json")

    assert response.status_code == 200
    data = response.json()
    assert data["question"] == "Updated Question"
    assert data["is_mandatory"] is True
    assert data["order"] == 2
    assert Decimal(data["positive_weight"]) == Decimal("3.00")
    assert data["is_fatal"] is True

    # Verify question was updated
    question.refresh_from_db()
    assert question.question == "Updated Question"
    assert question.is_fatal is True


def test_update_ft_question_not_found(organization: Organization, organization_owner_client: Client) -> None:
    """Test that updating a non-existent question returns 404."""
    from uuid import uuid4

    questionnaire = Questionnaire.objects.create(name="Test Questionnaire")
    org_questionnaire = OrganizationQuestionnaire.objects.create(organization=organization, questionnaire=questionnaire)

    payload = FreeTextQuestionUpdateSchema(question="Updated Question")

    url = reverse(
        "api:update_ft_question", kwargs={"org_questionnaire_id": org_questionnaire.id, "question_id": uuid4()}
    )
    response = organization_owner_client.put(url, data=payload.model_dump_json(), content_type="application/json")

    assert response.status_code == 404


# --- Delete FT question tests ---


def test_delete_ft_question_success(organization: Organization, organization_owner_client: Client) -> None:
    """Test that a free text question can be deleted."""
    questionnaire = Questionnaire.objects.create(name="Test Questionnaire")
    org_questionnaire = OrganizationQuestionnaire.objects.create(organization=organization, questionnaire=questionnaire)
    question = FreeTextQuestion.objects.create(
        questionnaire=questionnaire, question="Test Question", order=1, positive_weight=Decimal("1.0")
    )

    url = reverse(
        "api:delete_ft_question", kwargs={"org_questionnaire_id": org_questionnaire.id, "question_id": question.id}
    )
    response = organization_owner_client.delete(url)

    assert response.status_code == 204
    assert not FreeTextQuestion.objects.filter(id=question.id).exists()


def test_delete_ft_question_not_found(organization: Organization, organization_owner_client: Client) -> None:
    """Test that deleting a non-existent FT question returns 404."""
    from uuid import uuid4

    questionnaire = Questionnaire.objects.create(name="Test Questionnaire")
    org_questionnaire = OrganizationQuestionnaire.objects.create(organization=organization, questionnaire=questionnaire)

    url = reverse(
        "api:delete_ft_question", kwargs={"org_questionnaire_id": org_questionnaire.id, "question_id": uuid4()}
    )
    response = organization_owner_client.delete(url)

    assert response.status_code == 404


def test_delete_ft_question_permission_denied(organization: Organization, nonmember_client: Client) -> None:
    """Test that non-members cannot delete FT questions."""
    questionnaire = Questionnaire.objects.create(name="Test Questionnaire")
    org_questionnaire = OrganizationQuestionnaire.objects.create(organization=organization, questionnaire=questionnaire)
    question = FreeTextQuestion.objects.create(
        questionnaire=questionnaire, question="Test Question", order=1, positive_weight=Decimal("1.0")
    )

    url = reverse(
        "api:delete_ft_question", kwargs={"org_questionnaire_id": org_questionnaire.id, "question_id": question.id}
    )
    response = nonmember_client.delete(url)

    assert response.status_code == 404
