"""Tests for nested conditional questionnaire structures.

Tests for creating questions with nested conditional questions, sections, and
complex questionnaire structures.
"""

import pytest
from django.test import Client
from django.urls import reverse

from events.models import Organization, OrganizationQuestionnaire
from questionnaires.models import (
    FreeTextQuestion,
    MultipleChoiceQuestion,
    Questionnaire,
    QuestionnaireSection,
)
from questionnaires.schema import (
    FreeTextQuestionCreateSchema,
    MultipleChoiceOptionCreateSchema,
    MultipleChoiceQuestionCreateSchema,
    SectionCreateSchema,
)

pytestmark = pytest.mark.django_db


def test_create_mc_question_with_nested_conditional_questions(
    organization: Organization, organization_owner_client: Client
) -> None:
    """Test creating an MC question with nested conditional questions via API."""
    questionnaire = Questionnaire.objects.create(name="Test Questionnaire")
    org_questionnaire = OrganizationQuestionnaire.objects.create(organization=organization, questionnaire=questionnaire)

    # Q1: "Do you have allergies?" with nested conditional Q2 under "Yes" option
    payload = MultipleChoiceQuestionCreateSchema(
        question="Do you have allergies?",
        order=1,
        options=[
            MultipleChoiceOptionCreateSchema(
                option="Yes",
                is_correct=False,  # Branching question, no correct answer
                order=1,
                conditional_mc_questions=[
                    MultipleChoiceQuestionCreateSchema(
                        question="Are any life-threatening?",
                        order=2,
                        is_mandatory=True,
                        options=[
                            MultipleChoiceOptionCreateSchema(option="Yes", is_correct=False, order=1),
                            MultipleChoiceOptionCreateSchema(option="No", is_correct=False, order=2),
                        ],
                    ),
                ],
            ),
            MultipleChoiceOptionCreateSchema(option="No", is_correct=False, order=2),
        ],
    )

    url = reverse("api:create_mc_question", kwargs={"org_questionnaire_id": org_questionnaire.id})
    response = organization_owner_client.post(url, data=payload.model_dump_json(), content_type="application/json")

    assert response.status_code == 200

    # Verify Q1 was created
    q1 = MultipleChoiceQuestion.objects.get(questionnaire=questionnaire, question="Do you have allergies?")
    assert q1.options.count() == 2
    yes_option = q1.options.get(option="Yes")

    # Verify Q2 was created with depends_on_option set
    q2 = MultipleChoiceQuestion.objects.get(questionnaire=questionnaire, question="Are any life-threatening?")
    assert q2.depends_on_option == yes_option
    assert q2.is_mandatory is True
    assert q2.options.count() == 2


def test_create_mc_question_with_nested_conditional_ft_questions(
    organization: Organization, organization_owner_client: Client
) -> None:
    """Test creating an MC question with nested conditional free-text questions via API."""
    questionnaire = Questionnaire.objects.create(name="Test Questionnaire")
    org_questionnaire = OrganizationQuestionnaire.objects.create(organization=organization, questionnaire=questionnaire)

    payload = MultipleChoiceQuestionCreateSchema(
        question="Do you need additional information?",
        order=1,
        options=[
            MultipleChoiceOptionCreateSchema(
                option="Yes",
                is_correct=True,  # Only one correct
                order=1,
                conditional_ft_questions=[
                    FreeTextQuestionCreateSchema(
                        question="Please describe what you need.",
                        order=2,
                        is_mandatory=True,
                    ),
                ],
            ),
            MultipleChoiceOptionCreateSchema(option="No", is_correct=False, order=2),
        ],
    )

    url = reverse("api:create_mc_question", kwargs={"org_questionnaire_id": org_questionnaire.id})
    response = organization_owner_client.post(url, data=payload.model_dump_json(), content_type="application/json")

    assert response.status_code == 200

    # Verify MC question was created
    mc_q = MultipleChoiceQuestion.objects.get(questionnaire=questionnaire)
    yes_option = mc_q.options.get(option="Yes")

    # Verify FT question was created with depends_on_option set
    ft_q = FreeTextQuestion.objects.get(questionnaire=questionnaire)
    assert ft_q.depends_on_option == yes_option
    assert ft_q.is_mandatory is True


def test_create_mc_question_with_nested_conditional_section(
    organization: Organization, organization_owner_client: Client
) -> None:
    """Test creating an MC question with a nested conditional section via API."""
    questionnaire = Questionnaire.objects.create(name="Test Questionnaire")
    org_questionnaire = OrganizationQuestionnaire.objects.create(organization=organization, questionnaire=questionnaire)

    payload = MultipleChoiceQuestionCreateSchema(
        question="Do you want to provide details?",
        order=1,
        options=[
            MultipleChoiceOptionCreateSchema(
                option="Yes",
                is_correct=True,
                order=1,
                conditional_sections=[
                    SectionCreateSchema(
                        name="Additional Details",
                        order=1,
                        multiplechoicequestion_questions=[
                            MultipleChoiceQuestionCreateSchema(
                                question="What type of details?",
                                order=2,
                                options=[
                                    MultipleChoiceOptionCreateSchema(option="Technical", is_correct=True, order=1),
                                    MultipleChoiceOptionCreateSchema(option="General", is_correct=False, order=2),
                                ],
                            ),
                        ],
                        freetextquestion_questions=[
                            FreeTextQuestionCreateSchema(
                                question="Describe your situation.",
                                order=3,
                            ),
                        ],
                    ),
                ],
            ),
            MultipleChoiceOptionCreateSchema(option="No", is_correct=False, order=2),
        ],
    )

    url = reverse("api:create_mc_question", kwargs={"org_questionnaire_id": org_questionnaire.id})
    response = organization_owner_client.post(url, data=payload.model_dump_json(), content_type="application/json")

    assert response.status_code == 200

    # Verify Q1 was created
    q1 = MultipleChoiceQuestion.objects.get(questionnaire=questionnaire, question="Do you want to provide details?")
    yes_option = q1.options.get(option="Yes")

    # Verify section was created with depends_on_option set
    section = QuestionnaireSection.objects.get(questionnaire=questionnaire, name="Additional Details")
    assert section.depends_on_option == yes_option

    # Verify questions inside section
    q2 = MultipleChoiceQuestion.objects.get(questionnaire=questionnaire, question="What type of details?")
    assert q2.section == section

    ft_q = FreeTextQuestion.objects.get(questionnaire=questionnaire, question="Describe your situation.")
    assert ft_q.section == section


def test_create_complex_nested_conditional_questionnaire(
    organization: Organization, organization_owner_client: Client
) -> None:
    """Test creating a complex nested conditional structure via API.

    Structure:
    Q1: "Pick a track" (Technical / General)
      - Technical ->
          Q2: "What level?" (Beginner / Advanced)
            - Advanced ->
                Section: "Advanced Topics"
                  - Q3: "Which framework?" (MC)
                  - Q4: "Describe your experience" (FT, mandatory)
      - General ->
          Q5: "How did you hear about us?" (FT)
    """
    questionnaire = Questionnaire.objects.create(name="Complex Questionnaire")
    org_questionnaire = OrganizationQuestionnaire.objects.create(organization=organization, questionnaire=questionnaire)

    payload = MultipleChoiceQuestionCreateSchema(
        question="Pick a track",
        order=1,
        options=[
            MultipleChoiceOptionCreateSchema(
                option="Technical",
                is_correct=False,
                order=1,
                conditional_mc_questions=[
                    MultipleChoiceQuestionCreateSchema(
                        question="What level?",
                        order=2,
                        options=[
                            MultipleChoiceOptionCreateSchema(option="Beginner", is_correct=False, order=1),
                            MultipleChoiceOptionCreateSchema(
                                option="Advanced",
                                is_correct=False,
                                order=2,
                                conditional_sections=[
                                    SectionCreateSchema(
                                        name="Advanced Topics",
                                        order=1,
                                        multiplechoicequestion_questions=[
                                            MultipleChoiceQuestionCreateSchema(
                                                question="Which framework?",
                                                order=3,
                                                options=[
                                                    MultipleChoiceOptionCreateSchema(
                                                        option="Django", is_correct=True, order=1
                                                    ),
                                                    MultipleChoiceOptionCreateSchema(
                                                        option="FastAPI", is_correct=False, order=2
                                                    ),
                                                ],
                                            ),
                                        ],
                                        freetextquestion_questions=[
                                            FreeTextQuestionCreateSchema(
                                                question="Describe your experience",
                                                order=4,
                                                is_mandatory=True,
                                            ),
                                        ],
                                    ),
                                ],
                            ),
                        ],
                    ),
                ],
            ),
            MultipleChoiceOptionCreateSchema(
                option="General",
                is_correct=False,
                order=2,
                conditional_ft_questions=[
                    FreeTextQuestionCreateSchema(
                        question="How did you hear about us?",
                        order=5,
                    ),
                ],
            ),
        ],
    )

    url = reverse("api:create_mc_question", kwargs={"org_questionnaire_id": org_questionnaire.id})
    response = organization_owner_client.post(url, data=payload.model_dump_json(), content_type="application/json")

    assert response.status_code == 200

    # Verify structure
    q1 = MultipleChoiceQuestion.objects.get(questionnaire=questionnaire, question="Pick a track")
    technical_option = q1.options.get(option="Technical")
    general_option = q1.options.get(option="General")

    # Q2 depends on Technical
    q2 = MultipleChoiceQuestion.objects.get(questionnaire=questionnaire, question="What level?")
    assert q2.depends_on_option == technical_option
    advanced_option = q2.options.get(option="Advanced")

    # Section depends on Advanced
    section = QuestionnaireSection.objects.get(questionnaire=questionnaire, name="Advanced Topics")
    assert section.depends_on_option == advanced_option

    # Q3 and Q4 are in the section
    q3 = MultipleChoiceQuestion.objects.get(questionnaire=questionnaire, question="Which framework?")
    assert q3.section == section

    q4 = FreeTextQuestion.objects.get(questionnaire=questionnaire, question="Describe your experience")
    assert q4.section == section
    assert q4.is_mandatory is True

    # Q5 depends on General
    q5 = FreeTextQuestion.objects.get(questionnaire=questionnaire, question="How did you hear about us?")
    assert q5.depends_on_option == general_option

    # Total counts
    assert MultipleChoiceQuestion.objects.filter(questionnaire=questionnaire).count() == 3
    assert FreeTextQuestion.objects.filter(questionnaire=questionnaire).count() == 2
    assert QuestionnaireSection.objects.filter(questionnaire=questionnaire).count() == 1
