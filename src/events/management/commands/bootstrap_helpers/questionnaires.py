# src/events/management/commands/bootstrap_helpers/questionnaires.py
"""Questionnaire creation for bootstrap process."""

from datetime import timedelta
from decimal import Decimal

import structlog

from events import models as events_models
from questionnaires import models as questionnaires_models

from .base import BootstrapState

logger = structlog.get_logger(__name__)


def create_questionnaires(state: BootstrapState) -> None:
    """Create varied questionnaires with different evaluation modes."""
    logger.info("Creating questionnaires...")

    _create_code_of_conduct_questionnaire(state)
    _create_wine_tasting_questionnaire(state)
    _create_membership_questionnaire(state)
    _create_feedback_questionnaire(state)

    logger.info("Created 4 questionnaires with different evaluation modes")


def _create_code_of_conduct_questionnaire(state: BootstrapState) -> None:
    """Create simple Code of Conduct questionnaire for tech conference."""
    coc_questionnaire = questionnaires_models.Questionnaire.objects.create(
        name="Code of Conduct Agreement",
        status=questionnaires_models.Questionnaire.QuestionnaireStatus.PUBLISHED,
        evaluation_mode=questionnaires_models.Questionnaire.QuestionnaireEvaluationMode.AUTOMATIC,
        shuffle_questions=False,
        llm_backend=questionnaires_models.Questionnaire.QuestionnaireLLMBackend.MOCK,
        max_attempts=3,
        min_score=Decimal("100.00"),
    )

    coc_section = questionnaires_models.QuestionnaireSection.objects.create(
        questionnaire=coc_questionnaire,
        name="Community Guidelines",
        order=1,
    )

    coc_question = questionnaires_models.MultipleChoiceQuestion.objects.create(
        questionnaire=coc_questionnaire,
        section=coc_section,
        question=(
            "Do you agree to abide by our Code of Conduct, which includes treating all attendees "
            "with respect, refraining from harassment, and creating an inclusive environment?"
        ),
        allow_multiple_answers=False,
        shuffle_options=False,
        positive_weight=1,
        negative_weight=0,
        is_fatal=True,
        is_mandatory=True,
        order=1,
    )

    questionnaires_models.MultipleChoiceOption.objects.create(
        question=coc_question,
        option="Yes, I agree to the Code of Conduct",
        is_correct=True,
        order=1,
    )

    questionnaires_models.MultipleChoiceOption.objects.create(
        question=coc_question,
        option="No, I do not agree",
        is_correct=False,
        order=2,
    )

    # Link to tech conference
    org_quest_coc = events_models.OrganizationQuestionnaire.objects.create(
        organization=state.orgs["beta"],
        questionnaire=coc_questionnaire,
    )
    org_quest_coc.events.add(state.events["tech_conference"])


def _create_wine_tasting_questionnaire(state: BootstrapState) -> None:
    """Create Wine Tasting Application questionnaire for private event."""
    wine_questionnaire = questionnaires_models.Questionnaire.objects.create(
        name="Wine Tasting Dinner Application",
        status=questionnaires_models.Questionnaire.QuestionnaireStatus.PUBLISHED,
        evaluation_mode=questionnaires_models.Questionnaire.QuestionnaireEvaluationMode.MANUAL,
        shuffle_questions=False,
        llm_guidelines="Evaluate applicants based on genuine interest in wine and culinary experiences.",
        llm_backend=questionnaires_models.Questionnaire.QuestionnaireLLMBackend.MOCK,
        max_attempts=1,
        min_score=Decimal("60.00"),
    )

    wine_section = questionnaires_models.QuestionnaireSection.objects.create(
        questionnaire=wine_questionnaire,
        name="About You",
        order=1,
    )

    # CoC for wine event
    wine_coc = questionnaires_models.MultipleChoiceQuestion.objects.create(
        questionnaire=wine_questionnaire,
        section=wine_section,
        question="Do you agree to our Code of Conduct?",
        allow_multiple_answers=False,
        shuffle_options=False,
        positive_weight=1,
        negative_weight=0,
        is_fatal=True,
        is_mandatory=True,
        order=1,
    )

    questionnaires_models.MultipleChoiceOption.objects.create(
        question=wine_coc,
        option="Yes",
        is_correct=True,
        order=1,
    )

    questionnaires_models.MultipleChoiceOption.objects.create(
        question=wine_coc,
        option="No",
        is_correct=False,
        order=2,
    )

    # Interest question
    questionnaires_models.FreeTextQuestion.objects.create(
        questionnaire=wine_questionnaire,
        section=wine_section,
        question="What draws you to this wine tasting experience? Share your interest in wine or culinary arts.",
        llm_guidelines=(
            "Look for genuine enthusiasm and interest. Sophistication is not required - "
            "curiosity and appreciation matter most."
        ),
        positive_weight=3,
        negative_weight=0,
        is_fatal=False,
        is_mandatory=True,
        order=2,
    )

    # Experience level
    experience_q = questionnaires_models.MultipleChoiceQuestion.objects.create(
        questionnaire=wine_questionnaire,
        section=wine_section,
        question="How would you describe your wine knowledge?",
        allow_multiple_answers=False,
        shuffle_options=False,
        positive_weight=1,
        negative_weight=0,
        is_fatal=False,
        is_mandatory=True,
        order=3,
    )

    experience_options: dict[str, questionnaires_models.MultipleChoiceOption] = {}
    for idx, option in enumerate(
        [
            "Beginner - I'm curious to learn",
            "Intermediate - I enjoy wine regularly",
            "Advanced - I'm a serious enthusiast",
        ],
        1,
    ):
        opt = questionnaires_models.MultipleChoiceOption.objects.create(
            question=experience_q,
            option=option,
            is_correct=False,  # Doesnt matter
            order=idx,
        )
        experience_options[option] = opt

    # Conditional question: shown only if "Advanced" is selected
    questionnaires_models.FreeTextQuestion.objects.create(
        questionnaire=wine_questionnaire,
        section=wine_section,
        question="As an advanced wine enthusiast, which regions or varietals do you specialize in?",
        hint="Share your areas of expertise - this helps us tailor the experience for you.",
        llm_guidelines=(
            "Look for genuine expertise and passion. The answer should demonstrate "
            "real knowledge of wine regions, grape varieties, or winemaking techniques."
        ),
        positive_weight=2,
        negative_weight=0,
        is_fatal=False,
        is_mandatory=True,  # Mandatory IF shown (condition met)
        order=4,
        depends_on_option=experience_options["Advanced - I'm a serious enthusiast"],
    )

    # Link to wine tasting
    org_quest_wine = events_models.OrganizationQuestionnaire.objects.create(
        organization=state.orgs["alpha"],
        questionnaire=wine_questionnaire,
    )
    org_quest_wine.events.add(state.events["wine_tasting"])


def _create_membership_questionnaire(state: BootstrapState) -> None:
    """Create Community Membership Application questionnaire (org-level)."""
    membership_questionnaire = questionnaires_models.Questionnaire.objects.create(
        name="Tech Innovators Network Membership Application",
        status=questionnaires_models.Questionnaire.QuestionnaireStatus.PUBLISHED,
        evaluation_mode=questionnaires_models.Questionnaire.QuestionnaireEvaluationMode.HYBRID,
        shuffle_questions=False,
        llm_guidelines=(
            "Evaluate based on genuine interest in technology, community contribution mindset, "
            "and professional background. We want diverse perspectives and skill levels."
        ),
        llm_backend=questionnaires_models.Questionnaire.QuestionnaireLLMBackend.MOCK,
        max_attempts=2,
        can_retake_after=timedelta(days=30),
        min_score=Decimal("70.00"),
    )

    member_section1 = questionnaires_models.QuestionnaireSection.objects.create(
        questionnaire=membership_questionnaire,
        name="Professional Background",
        order=1,
    )

    member_section2 = questionnaires_models.QuestionnaireSection.objects.create(
        questionnaire=membership_questionnaire,
        name="Community Fit",
        order=2,
    )

    # Section 1 questions
    questionnaires_models.FreeTextQuestion.objects.create(
        questionnaire=membership_questionnaire,
        section=member_section1,
        question="Tell us about your professional background and current work in tech.",
        llm_guidelines=(
            "Look for clear communication and genuine tech involvement. "
            "All experience levels welcome - from students to seniors."
        ),
        positive_weight=2,
        negative_weight=0,
        is_fatal=False,
        is_mandatory=True,
        order=1,
    )

    tech_areas = questionnaires_models.MultipleChoiceQuestion.objects.create(
        questionnaire=membership_questionnaire,
        section=member_section1,
        question="Which tech areas are you most interested in? (Select all that apply)",
        allow_multiple_answers=True,
        shuffle_options=False,
        positive_weight=1,
        negative_weight=0,
        is_fatal=False,
        is_mandatory=True,
        order=2,
    )

    tech_area_options: dict[str, questionnaires_models.MultipleChoiceOption] = {}
    for idx, area in enumerate(
        [
            "AI/Machine Learning",
            "Web Development",
            "Mobile Development",
            "DevOps/Infrastructure",
            "Security",
            "Blockchain/Web3",
            "Data Science",
            "Other",
        ],
        1,
    ):
        opt = questionnaires_models.MultipleChoiceOption.objects.create(
            question=tech_areas,
            option=area,
            is_correct=True,
            order=idx,
        )
        tech_area_options[area] = opt

    # Conditional question: shown only if "AI/Machine Learning" is selected
    ai_followup_q = questionnaires_models.MultipleChoiceQuestion.objects.create(
        questionnaire=membership_questionnaire,
        section=member_section1,
        question="Which AI/ML areas interest you most? (Select all that apply)",
        hint="This helps us connect you with relevant community members and events.",
        allow_multiple_answers=True,
        shuffle_options=True,
        positive_weight=1,
        negative_weight=0,
        is_fatal=False,
        is_mandatory=True,  # Mandatory IF shown (condition met)
        order=3,
        depends_on_option=tech_area_options["AI/Machine Learning"],
    )

    for idx, ai_area in enumerate(
        [
            "Large Language Models (LLMs)",
            "Computer Vision",
            "Reinforcement Learning",
            "MLOps & Model Deployment",
            "AI Ethics & Safety",
            "Generative AI (images, music, etc.)",
        ],
        1,
    ):
        questionnaires_models.MultipleChoiceOption.objects.create(
            question=ai_followup_q,
            option=ai_area,
            is_correct=True,
            order=idx,
        )

    # Conditional section: shown only if "Blockchain/Web3" is selected
    web3_section = questionnaires_models.QuestionnaireSection.objects.create(
        questionnaire=membership_questionnaire,
        name="Web3 Experience",
        description="Tell us more about your blockchain/Web3 background.",
        order=3,
        depends_on_option=tech_area_options["Blockchain/Web3"],
    )

    questionnaires_models.FreeTextQuestion.objects.create(
        questionnaire=membership_questionnaire,
        section=web3_section,
        question="Describe your experience with blockchain or Web3 technologies.",
        llm_guidelines="Look for genuine interest or experience. Beginners are welcome too.",
        positive_weight=1,
        negative_weight=0,
        is_fatal=False,
        is_mandatory=True,
        order=1,
    )

    web3_chains_q = questionnaires_models.MultipleChoiceQuestion.objects.create(
        questionnaire=membership_questionnaire,
        section=web3_section,
        question="Which blockchains have you worked with or are interested in?",
        allow_multiple_answers=True,
        shuffle_options=True,
        positive_weight=1,
        negative_weight=0,
        is_fatal=False,
        is_mandatory=False,
        order=2,
    )

    for idx, chain in enumerate(["Ethereum", "Solana", "Polygon", "Bitcoin", "Other L1/L2"], 1):
        questionnaires_models.MultipleChoiceOption.objects.create(
            question=web3_chains_q,
            option=chain,
            is_correct=True,
            order=idx,
        )

    # Section 2 questions
    questionnaires_models.FreeTextQuestion.objects.create(
        questionnaire=membership_questionnaire,
        section=member_section2,
        question="What would you like to contribute to our community? (e.g., skills, knowledge, time, ideas)",
        llm_guidelines="Look for willingness to participate and contribute. Community is about give-and-take.",
        positive_weight=3,
        negative_weight=0,
        is_fatal=False,
        is_mandatory=True,
        order=1,
    )

    coc_member = questionnaires_models.MultipleChoiceQuestion.objects.create(
        questionnaire=membership_questionnaire,
        section=member_section2,
        question="Do you commit to fostering an inclusive, respectful community?",
        allow_multiple_answers=False,
        shuffle_options=False,
        positive_weight=1,
        negative_weight=0,
        is_fatal=True,
        is_mandatory=True,
        order=2,
    )

    questionnaires_models.MultipleChoiceOption.objects.create(
        question=coc_member,
        option="Yes, I commit to these values",
        is_correct=True,
        order=1,
    )

    questionnaires_models.MultipleChoiceOption.objects.create(
        question=coc_member,
        option="No",
        is_correct=False,
        order=2,
    )

    # Link to organization (not specific events)
    events_models.OrganizationQuestionnaire.objects.create(
        organization=state.orgs["beta"],
        questionnaire=membership_questionnaire,
    )


def _create_feedback_questionnaire(state: BootstrapState) -> None:
    """Create simple feedback questionnaire for past event."""
    # Use MANUAL mode - feedback questionnaires skip evaluation anyway (handled by feedback_service)
    feedback_questionnaire = questionnaires_models.Questionnaire.objects.create(
        name="Event Feedback",
        status=questionnaires_models.Questionnaire.QuestionnaireStatus.PUBLISHED,
        evaluation_mode=questionnaires_models.Questionnaire.QuestionnaireEvaluationMode.MANUAL,
        shuffle_questions=False,
        llm_backend=questionnaires_models.Questionnaire.QuestionnaireLLMBackend.MOCK,
    )

    feedback_section = questionnaires_models.QuestionnaireSection.objects.create(
        questionnaire=feedback_questionnaire,
        name="Your Feedback",
        order=1,
    )

    # Simple yes/no question
    liked_event_q = questionnaires_models.MultipleChoiceQuestion.objects.create(
        questionnaire=feedback_questionnaire,
        section=feedback_section,
        question="Did you enjoy the event?",
        allow_multiple_answers=False,
        shuffle_options=False,
        positive_weight=1,
        negative_weight=0,
        is_fatal=False,
        is_mandatory=True,
        order=1,
    )

    questionnaires_models.MultipleChoiceOption.objects.create(
        question=liked_event_q,
        option="Yes, I loved it!",
        is_correct=True,
        order=1,
    )

    questionnaires_models.MultipleChoiceOption.objects.create(
        question=liked_event_q,
        option="No, it wasn't for me",
        is_correct=False,
        order=2,
    )

    # Optional free text for additional feedback
    questionnaires_models.FreeTextQuestion.objects.create(
        questionnaire=feedback_questionnaire,
        section=feedback_section,
        question="Any additional comments or suggestions?",
        hint="We'd love to hear your thoughts to improve future events.",
        positive_weight=1,
        negative_weight=0,
        is_fatal=False,
        is_mandatory=False,
        order=2,
    )

    # Link to past event as FEEDBACK type
    org_quest_feedback = events_models.OrganizationQuestionnaire.objects.create(
        organization=state.orgs["alpha"],
        questionnaire=feedback_questionnaire,
        questionnaire_type=events_models.OrganizationQuestionnaire.QuestionnaireType.FEEDBACK,
    )
    org_quest_feedback.events.add(state.events["past_event"])
