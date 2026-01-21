"""Questionnaire seeding module."""

from decimal import Decimal

from django.utils import timezone

from events.management.commands.seeder.base import BaseSeeder
from events.models import OrganizationQuestionnaire
from questionnaires.models import (
    FreeTextAnswer,
    FreeTextQuestion,
    MultipleChoiceAnswer,
    MultipleChoiceOption,
    MultipleChoiceQuestion,
    Questionnaire,
    QuestionnaireEvaluation,
    QuestionnaireSection,
    QuestionnaireSubmission,
)

# Question templates
MC_QUESTIONS = [
    "Have you attended similar events before?",
    "How did you hear about us?",
    "Do you agree to our code of conduct?",
    "What is your experience level?",
    "Which topics interest you most?",
    "Would you recommend us to others?",
]

FT_QUESTIONS = [
    "Tell us about yourself and your interest in our community.",
    "What do you hope to gain from this experience?",
    "Describe your relevant background or expertise.",
    "Is there anything we should know about you?",
    "What are your expectations for this event?",
]


class QuestionnaireSeeder(BaseSeeder):
    """Seeder for questionnaires and related models."""

    def seed(self) -> None:
        """Seed questionnaires and related entities."""
        self._create_questionnaires()
        self._create_sections_and_questions()
        self._create_org_questionnaires()
        self._create_submissions()
        self._create_evaluations()

    def _create_questionnaires(self) -> None:
        """Create questionnaires with various configurations."""
        self.log("Creating questionnaires...")

        questionnaires_to_create: list[Questionnaire] = []
        min_q, max_q = self.config.questionnaires_per_org

        for org in self.state.organizations:
            num_q = self.random_int(min_q, max_q)

            for i in range(num_q):
                status = self.random_choice(list(Questionnaire.QuestionnaireStatus.values))
                eval_mode = self.random_choice(list(Questionnaire.QuestionnaireEvaluationMode.values))
                llm_backend = self.random_choice(list(Questionnaire.QuestionnaireLLMBackend.values))

                q = Questionnaire(
                    name=f"{org.name} Questionnaire {i}",
                    description=self.faker.paragraph() if self.random_bool(0.7) else "",
                    min_score=Decimal(self.random_int(50, 80)),
                    status=status,
                    evaluation_mode=eval_mode,
                    llm_backend=llm_backend,
                    shuffle_questions=self.random_bool(0.3),
                    shuffle_sections=self.random_bool(0.2),
                    max_attempts=self.random_choice([0, 1, 3, 5]),
                )
                questionnaires_to_create.append(q)

        self.state.questionnaires = self.batch_create(
            Questionnaire, questionnaires_to_create, desc="Creating questionnaires"
        )
        self.log(f"  Created {len(self.state.questionnaires)} questionnaires")

    def _create_sections_and_questions(self) -> None:
        """Create sections and questions for questionnaires."""
        self.log("Creating sections and questions...")

        sections_to_create: list[QuestionnaireSection] = []
        mc_questions_to_create: list[MultipleChoiceQuestion] = []
        ft_questions_to_create: list[FreeTextQuestion] = []
        mc_options_to_create: list[MultipleChoiceOption] = []

        for questionnaire in self.state.questionnaires:
            # Create 1-3 sections per questionnaire
            num_sections = self.random_int(1, 3)

            for s in range(num_sections):
                section = QuestionnaireSection(
                    questionnaire=questionnaire,
                    name=f"Section {s + 1}",
                    description=self.faker.sentence() if self.random_bool(0.5) else "",
                    order=s,
                )
                sections_to_create.append(section)

            # Create 3-8 questions per questionnaire (mix of MC and FT)
            num_questions = self.random_int(3, 8)
            mc_count = self.random_int(2, min(num_questions - 1, 5))
            ft_count = num_questions - mc_count

            # Multiple choice questions
            mc_templates = self.random_sample(MC_QUESTIONS, mc_count)
            for q_idx, template in enumerate(mc_templates):
                mc_q = MultipleChoiceQuestion(
                    questionnaire=questionnaire,
                    question=template,
                    hint=self.faker.sentence() if self.random_bool(0.3) else "",
                    order=q_idx,
                    is_mandatory=self.random_bool(0.6),
                    is_fatal=self.random_bool(0.1),
                    allow_multiple_answers=self.random_bool(0.3),
                    shuffle_options=self.random_bool(0.7),
                    positive_weight=Decimal(self.random_int(1, 3)),
                    negative_weight=Decimal(0),
                )
                mc_questions_to_create.append(mc_q)

            # Free text questions
            ft_templates = self.random_sample(FT_QUESTIONS, ft_count)
            for q_idx, template in enumerate(ft_templates):
                ft_q = FreeTextQuestion(
                    questionnaire=questionnaire,
                    question=template,
                    hint=self.faker.sentence() if self.random_bool(0.3) else "",
                    order=mc_count + q_idx,
                    is_mandatory=self.random_bool(0.5),
                    is_fatal=False,
                    positive_weight=Decimal(self.random_int(1, 5)),
                    negative_weight=Decimal(0),
                )
                ft_questions_to_create.append(ft_q)

        # Batch create sections first
        self.batch_create(QuestionnaireSection, sections_to_create, desc="Creating sections")

        # Batch create MC questions
        created_mc = self.batch_create(
            MultipleChoiceQuestion,
            mc_questions_to_create,
            desc="Creating MC questions",
        )

        # Batch create FT questions
        self.batch_create(FreeTextQuestion, ft_questions_to_create, desc="Creating FT questions")

        # Create options for MC questions (3-5 per question)
        for mc_q in created_mc:
            num_options = self.random_int(3, 5)
            correct_idx = self.random_int(0, num_options - 1)

            for opt_idx in range(num_options):
                mc_options_to_create.append(
                    MultipleChoiceOption(
                        question=mc_q,
                        option=f"Option {opt_idx + 1}: {self.faker.word()}",
                        is_correct=(opt_idx == correct_idx),
                        order=opt_idx,
                    )
                )

        self.batch_create(MultipleChoiceOption, mc_options_to_create, desc="Creating MC options")

        self.log(f"  Created {len(sections_to_create)} sections")
        self.log(f"  Created {len(created_mc)} MC questions")
        self.log(f"  Created {len(ft_questions_to_create)} FT questions")
        self.log(f"  Created {len(mc_options_to_create)} MC options")

    def _create_org_questionnaires(self) -> None:
        """Link questionnaires to organizations."""
        self.log("Creating organization questionnaires...")

        org_questionnaires_to_create: list[OrganizationQuestionnaire] = []

        # Distribute questionnaires to orgs
        q_idx = 0
        for org in self.state.organizations:
            num_q = self.random_int(1, 3)
            org_qs: list[Questionnaire] = []

            for _ in range(num_q):
                if q_idx >= len(self.state.questionnaires):
                    break

                questionnaire = self.state.questionnaires[q_idx]
                q_type = self.random_choice(list(OrganizationQuestionnaire.QuestionnaireType.values))

                org_questionnaires_to_create.append(
                    OrganizationQuestionnaire(
                        organization=org,
                        questionnaire=questionnaire,
                        questionnaire_type=q_type,
                        members_exempt=self.random_bool(0.3),
                    )
                )
                org_qs.append(questionnaire)
                q_idx += 1

            self.state.org_questionnaires[org.id] = org_qs

        self.batch_create(
            OrganizationQuestionnaire,
            org_questionnaires_to_create,
            desc="Creating org questionnaires",
        )
        self.log(f"  Created {len(org_questionnaires_to_create)} org questionnaires")

    def _create_submissions(self) -> None:
        """Create questionnaire submissions with answers."""
        self.log("Creating questionnaire submissions...")

        submissions_to_create: list[QuestionnaireSubmission] = []
        mc_answers_to_create: list[MultipleChoiceAnswer] = []
        ft_answers_to_create: list[FreeTextAnswer] = []

        for questionnaire in self.state.questionnaires:
            # 10-30 submissions per questionnaire
            num_submissions = self.random_int(10, 30)
            submitting_users = self.random_sample(self.state.users, min(num_submissions, len(self.state.users)))

            for user in submitting_users:
                status = self.random_choice(list(QuestionnaireSubmission.QuestionnaireSubmissionStatus.values))

                submission = QuestionnaireSubmission(
                    questionnaire=questionnaire,
                    user=user,
                    status=status,
                    submitted_at=timezone.now() if status == "ready" else None,
                )
                submissions_to_create.append(submission)

        created_submissions = self.batch_create(
            QuestionnaireSubmission,
            submissions_to_create,
            desc="Creating submissions",
        )

        # Create answers for each submission
        self.log("Creating submission answers...")

        for submission in created_submissions:
            # Get questions for this questionnaire
            mc_questions = list(
                MultipleChoiceQuestion.objects.filter(questionnaire=submission.questionnaire).prefetch_related(
                    "options"
                )
            )
            ft_questions = list(FreeTextQuestion.objects.filter(questionnaire=submission.questionnaire))

            # Answer MC questions
            for mc_q in mc_questions:
                options = list(mc_q.options.all())
                if options:
                    selected_option = self.random_choice(options)
                    mc_answers_to_create.append(
                        MultipleChoiceAnswer(
                            submission=submission,
                            question=mc_q,
                            option=selected_option,
                        )
                    )

            # Answer FT questions
            for ft_q in ft_questions:
                ft_answers_to_create.append(
                    FreeTextAnswer(
                        submission=submission,
                        question=ft_q,
                        answer=self.faker.paragraph(),
                    )
                )

        self.batch_create(MultipleChoiceAnswer, mc_answers_to_create, desc="Creating MC answers")
        self.batch_create(FreeTextAnswer, ft_answers_to_create, desc="Creating FT answers")

        self.log(f"  Created {len(created_submissions)} submissions")
        self.log(f"  Created {len(mc_answers_to_create)} MC answers")
        self.log(f"  Created {len(ft_answers_to_create)} FT answers")

    def _create_evaluations(self) -> None:
        """Create evaluations for ready submissions."""
        self.log("Creating questionnaire evaluations...")

        evaluations_to_create: list[QuestionnaireEvaluation] = []

        # Get ready submissions that don't already have evaluations
        ready_submissions = QuestionnaireSubmission.objects.filter(
            status="ready",
            evaluation__isnull=True,
        )

        for submission in ready_submissions:
            status_key = self.weighted_choice(self.config.evaluation_status_weights)
            status_map = {
                "approved": QuestionnaireEvaluation.QuestionnaireEvaluationStatus.APPROVED,
                "rejected": QuestionnaireEvaluation.QuestionnaireEvaluationStatus.REJECTED,
                "pending_review": QuestionnaireEvaluation.QuestionnaireEvaluationStatus.PENDING_REVIEW,
            }
            status = status_map[status_key]

            # Generate a score based on status
            if status == QuestionnaireEvaluation.QuestionnaireEvaluationStatus.APPROVED:
                score = Decimal(self.random_int(70, 100))
            elif status == QuestionnaireEvaluation.QuestionnaireEvaluationStatus.REJECTED:
                score = Decimal(self.random_int(0, 50))
            else:
                score = Decimal(self.random_int(40, 70))

            evaluations_to_create.append(
                QuestionnaireEvaluation(
                    submission=submission,
                    score=score,
                    status=status,
                    proposed_status=self.random_choice(["approved", "rejected"])
                    if status == QuestionnaireEvaluation.QuestionnaireEvaluationStatus.PENDING_REVIEW
                    else None,
                    automatically_evaluated=self.random_bool(0.7),
                    comments=self.faker.sentence() if self.random_bool(0.3) else "",
                )
            )

        self.batch_create(
            QuestionnaireEvaluation,
            evaluations_to_create,
            desc="Creating evaluations",
        )
        self.log(f"  Created {len(evaluations_to_create)} evaluations")
