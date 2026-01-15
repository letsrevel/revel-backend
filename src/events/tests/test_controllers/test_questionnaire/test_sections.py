"""Tests for questionnaire section CRUD operations."""

import pytest
from django.test import Client
from django.urls import reverse

from events.models import Organization, OrganizationQuestionnaire
from questionnaires.models import MultipleChoiceOption, MultipleChoiceQuestion, Questionnaire, QuestionnaireSection
from questionnaires.schema import SectionCreateSchema, SectionUpdateSchema

pytestmark = pytest.mark.django_db


# --- Create section tests ---


def test_create_section_success(organization: Organization, organization_owner_client: Client) -> None:
    """Test that a section can be created."""
    questionnaire = Questionnaire.objects.create(name="Test Questionnaire")
    org_questionnaire = OrganizationQuestionnaire.objects.create(organization=organization, questionnaire=questionnaire)

    payload = SectionCreateSchema(name="New Section", order=1)

    url = reverse("api:create_section", kwargs={"org_questionnaire_id": org_questionnaire.id})
    response = organization_owner_client.post(url, data=payload.model_dump_json(), content_type="application/json")

    assert response.status_code == 200
    data = response.json()
    assert data["name"] == "New Section"
    assert data["order"] == 1

    # Verify section was created
    section = QuestionnaireSection.objects.get(questionnaire=questionnaire, name="New Section")
    assert section.order == 1


def test_create_section_permission_denied(organization: Organization, nonmember_client: Client) -> None:
    """Test that non-members cannot create sections."""
    questionnaire = Questionnaire.objects.create(name="Test Questionnaire")
    org_questionnaire = OrganizationQuestionnaire.objects.create(organization=organization, questionnaire=questionnaire)

    payload = SectionCreateSchema(name="New Section", order=1)

    url = reverse("api:create_section", kwargs={"org_questionnaire_id": org_questionnaire.id})
    response = nonmember_client.post(url, data=payload.model_dump_json(), content_type="application/json")

    assert response.status_code == 403


# --- Update section tests ---


def test_update_section_success(organization: Organization, organization_owner_client: Client) -> None:
    """Test that a section can be updated."""
    questionnaire = Questionnaire.objects.create(name="Test Questionnaire")
    org_questionnaire = OrganizationQuestionnaire.objects.create(organization=organization, questionnaire=questionnaire)
    section = QuestionnaireSection.objects.create(questionnaire=questionnaire, name="Original Section", order=1)

    payload = SectionUpdateSchema(name="Updated Section", order=2)

    url = reverse("api:update_section", kwargs={"org_questionnaire_id": org_questionnaire.id, "section_id": section.id})
    response = organization_owner_client.put(url, data=payload.model_dump_json(), content_type="application/json")

    assert response.status_code == 200
    data = response.json()
    assert data["name"] == "Updated Section"
    assert data["order"] == 2

    # Verify section was updated
    section.refresh_from_db()
    assert section.name == "Updated Section"
    assert section.order == 2


def test_update_section_not_found(organization: Organization, organization_owner_client: Client) -> None:
    """Test that updating a non-existent section returns 404."""
    from uuid import uuid4

    questionnaire = Questionnaire.objects.create(name="Test Questionnaire")
    org_questionnaire = OrganizationQuestionnaire.objects.create(organization=organization, questionnaire=questionnaire)

    payload = SectionUpdateSchema(name="Updated Section", order=2)

    url = reverse("api:update_section", kwargs={"org_questionnaire_id": org_questionnaire.id, "section_id": uuid4()})
    response = organization_owner_client.put(url, data=payload.model_dump_json(), content_type="application/json")

    assert response.status_code == 404


# --- Delete section tests ---


def test_delete_section_success(organization: Organization, organization_owner_client: Client) -> None:
    """Test that a section can be deleted."""
    questionnaire = Questionnaire.objects.create(name="Test Questionnaire")
    org_questionnaire = OrganizationQuestionnaire.objects.create(organization=organization, questionnaire=questionnaire)
    section = QuestionnaireSection.objects.create(questionnaire=questionnaire, name="Test Section", order=1)

    url = reverse("api:delete_section", kwargs={"org_questionnaire_id": org_questionnaire.id, "section_id": section.id})
    response = organization_owner_client.delete(url)

    assert response.status_code == 204
    assert not QuestionnaireSection.objects.filter(id=section.id).exists()


def test_delete_section_not_found(organization: Organization, organization_owner_client: Client) -> None:
    """Test that deleting a non-existent section returns 404."""
    from uuid import uuid4

    questionnaire = Questionnaire.objects.create(name="Test Questionnaire")
    org_questionnaire = OrganizationQuestionnaire.objects.create(organization=organization, questionnaire=questionnaire)

    url = reverse("api:delete_section", kwargs={"org_questionnaire_id": org_questionnaire.id, "section_id": uuid4()})
    response = organization_owner_client.delete(url)

    assert response.status_code == 404


def test_delete_section_permission_denied(organization: Organization, nonmember_client: Client) -> None:
    """Test that non-members cannot delete sections."""
    questionnaire = Questionnaire.objects.create(name="Test Questionnaire")
    org_questionnaire = OrganizationQuestionnaire.objects.create(organization=organization, questionnaire=questionnaire)
    section = QuestionnaireSection.objects.create(questionnaire=questionnaire, name="Test Section", order=1)

    url = reverse("api:delete_section", kwargs={"org_questionnaire_id": org_questionnaire.id, "section_id": section.id})
    response = nonmember_client.delete(url)

    assert response.status_code == 404


# --- Create section with depends_on_option_id tests ---


def test_create_section_with_depends_on_option_id(
    organization: Organization, organization_owner_client: Client
) -> None:
    """Test that a conditional section can be created with depends_on_option_id."""
    questionnaire = Questionnaire.objects.create(name="Test Questionnaire")
    org_questionnaire = OrganizationQuestionnaire.objects.create(organization=organization, questionnaire=questionnaire)

    # Create a parent question with options
    parent_question = MultipleChoiceQuestion.objects.create(
        questionnaire=questionnaire, question="Do you have experience?", order=1
    )
    yes_option = MultipleChoiceOption.objects.create(question=parent_question, option="Yes", is_correct=True, order=1)

    # Create a conditional section that depends on the "Yes" option
    payload = SectionCreateSchema(
        name="Experience Details",
        depends_on_option_id=yes_option.id,
        order=1,
    )

    url = reverse("api:create_section", kwargs={"org_questionnaire_id": org_questionnaire.id})
    response = organization_owner_client.post(url, data=payload.model_dump_json(), content_type="application/json")

    assert response.status_code == 200
    data = response.json()
    assert data["name"] == "Experience Details"
    assert data["depends_on_option_id"] == str(yes_option.id)

    # Verify in database
    section = QuestionnaireSection.objects.get(id=data["id"])
    assert section.depends_on_option_id == yes_option.id


def test_create_section_with_invalid_depends_on_option_id(
    organization: Organization, organization_owner_client: Client
) -> None:
    """Test that creating section with option from another questionnaire fails."""
    questionnaire = Questionnaire.objects.create(name="Test Questionnaire")
    org_questionnaire = OrganizationQuestionnaire.objects.create(organization=organization, questionnaire=questionnaire)

    # Create option in a different questionnaire
    other_questionnaire = Questionnaire.objects.create(name="Other Questionnaire")
    other_question = MultipleChoiceQuestion.objects.create(
        questionnaire=other_questionnaire, question="Other Question", order=1
    )
    other_option = MultipleChoiceOption.objects.create(
        question=other_question, option="Other Option", is_correct=True, order=1
    )

    payload = SectionCreateSchema(
        name="Conditional Section",
        depends_on_option_id=other_option.id,
    )

    url = reverse("api:create_section", kwargs={"org_questionnaire_id": org_questionnaire.id})
    response = organization_owner_client.post(url, data=payload.model_dump_json(), content_type="application/json")

    # Should return 400 or 422 due to integrity error
    assert response.status_code in (400, 422)
