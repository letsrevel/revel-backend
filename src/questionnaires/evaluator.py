"""This module contains the business logic for evaluating questionnaire submissions."""

from decimal import Decimal
from uuid import UUID

import structlog
from django.db import transaction
from django.db.models import Prefetch

from questionnaires.llms.llm_interfaces import AnswerToEvaluate, EvaluationResponse, FreeTextEvaluator

from .models import (
    EvaluationAuditData,
    FreeTextQuestion,
    MultipleChoiceQuestion,
    Questionnaire,
    QuestionnaireEvaluation,
    QuestionnaireSection,
    QuestionnaireSubmission,
)

logger = structlog.get_logger(__name__)

# ---- The Evaluation Service Class ----


class SubmissionEvaluator:
    """A service class to encapsulate the logic for evaluating a questionnaire submission.

    This class orchestrates scoring for all question types, interacts with a
    pluggable LLM backend for free-text evaluation, and correctly sets the
    final or proposed status based on the questionnaire's evaluation mode.
    """

    def __init__(self, submission: QuestionnaireSubmission, llm_evaluator: FreeTextEvaluator | None = None):
        """Initialize the evaluator."""
        if submission.status != QuestionnaireSubmission.QuestionnaireSubmissionStatus.READY:
            raise ValueError("Only submitted questionnaires can be evaluated.")

        self.submission = submission
        self.llm_evaluator = llm_evaluator if llm_evaluator else submission.questionnaire.get_llm_backend()
        self.questionnaire = Questionnaire.objects.prefetch_related(
            Prefetch(
                "multiplechoicequestion_questions",
                queryset=MultipleChoiceQuestion.objects.prefetch_related("options"),
            ),
            Prefetch(
                "freetextquestion_questions",
                queryset=FreeTextQuestion.objects.all(),
            ),
            Prefetch(
                "sections",
                queryset=QuestionnaireSection.objects.all(),
            ),
        ).get(pk=submission.questionnaire_id)

        # Initialize state
        self.mc_points_scored = Decimal("0.0")
        self.max_mc_points = Decimal("0.0")
        self.ft_points_scored = Decimal("0.0")
        self.max_ft_points = Decimal("0.0")
        self.llm_batch_response: EvaluationResponse | None = None
        self.fatal_error = False
        self.missing_mandatory: list[UUID] = []

        # Compute applicable questions based on conditional dependencies
        self._selected_option_ids: set[UUID] = set()
        self._applicable_section_ids: set[UUID] = set()
        self._applicable_mcq_ids: set[UUID] = set()
        self._applicable_ftq_ids: set[UUID] = set()
        self._compute_applicable_questions()

    def _compute_applicable_questions(self) -> None:
        """Compute which questions are applicable based on conditional dependencies.

        A question is applicable if:
        1. It has no depends_on_option, OR its depends_on_option was selected
        2. Its section (if any) is also applicable

        A section is applicable if:
        1. It has no depends_on_option, OR its depends_on_option was selected
        """
        # Get all selected option IDs from the submission
        self._selected_option_ids = set(
            self.submission.multiplechoiceanswer_answers.values_list("option_id", flat=True)
        )

        # Determine applicable sections
        for section in self.questionnaire.sections.all():
            if section.depends_on_option_id is None or section.depends_on_option_id in self._selected_option_ids:
                self._applicable_section_ids.add(section.id)

        # Determine applicable MC questions
        for mc_question in self.questionnaire.multiplechoicequestion_questions.all():
            if self._is_question_applicable(mc_question):
                self._applicable_mcq_ids.add(mc_question.id)

        # Determine applicable FT questions
        for ft_question in self.questionnaire.freetextquestion_questions.all():
            if self._is_question_applicable(ft_question):
                self._applicable_ftq_ids.add(ft_question.id)

        logger.debug(
            "questionnaire_applicable_questions_computed",
            submission_id=str(self.submission.id),
            selected_options=len(self._selected_option_ids),
            applicable_sections=len(self._applicable_section_ids),
            applicable_mcq=len(self._applicable_mcq_ids),
            applicable_ftq=len(self._applicable_ftq_ids),
        )

    def _is_question_applicable(self, question: MultipleChoiceQuestion | FreeTextQuestion) -> bool:
        """Check if a question is applicable based on its dependencies."""
        # Check section applicability
        if question.section_id is not None and question.section_id not in self._applicable_section_ids:
            return False

        # Check direct option dependency
        if question.depends_on_option_id is not None and question.depends_on_option_id not in self._selected_option_ids:
            return False

        return True

    @transaction.atomic
    def evaluate(self) -> QuestionnaireEvaluation:
        """Main entry point to run the entire evaluation process.

        This method is atomic to ensure the evaluation is an all-or-nothing operation.
        """
        logger.info(
            "questionnaire_evaluation_started",
            submission_id=str(self.submission.id),
            questionnaire_id=str(self.questionnaire.id),
            user_id=str(self.submission.user_id),
        )
        # First, check for a hard failure condition: unanswered mandatory questions.
        self._check_for_missing_mandatory_answers()

        # We proceed with scoring to provide a partial score for audit purposes,
        # but the final result will be overridden if a mandatory question was missed.
        self._evaluate_mc_answers()
        self._evaluate_ft_answers_in_batch()
        evaluation = self._create_or_update_evaluation()
        logger.info(
            "questionnaire_evaluation_completed",
            submission_id=str(self.submission.id),
            evaluation_id=str(evaluation.id),
            score=float(evaluation.score or 0),
            status=evaluation.status,
        )
        return evaluation

    def _check_for_missing_mandatory_answers(self) -> None:
        """Checks if any applicable mandatory questions were left unanswered.

        Only checks questions that are applicable based on conditional dependencies.
        A mandatory question that wasn't shown (condition not met) doesn't fail the submission.
        """
        # Get IDs of all mandatory questions that are also applicable
        mandatory_mcq_ids = {
            q.id
            for q in self.questionnaire.multiplechoicequestion_questions.all()
            if q.is_mandatory and q.id in self._applicable_mcq_ids
        }
        mandatory_ftq_ids = {
            q.id
            for q in self.questionnaire.freetextquestion_questions.all()
            if q.is_mandatory and q.id in self._applicable_ftq_ids
        }

        # Get IDs of all answered questions from the submission
        answered_mcq_ids = set(self.submission.multiplechoiceanswer_answers.values_list("question_id", flat=True))
        answered_ftq_ids = set(self.submission.freetextanswer_answers.values_list("question_id", flat=True))

        # If the difference between the sets is not empty, a mandatory question was missed
        missing_mandatory_mcq = mandatory_mcq_ids - answered_mcq_ids
        missing_mandatory_ftq = mandatory_ftq_ids - answered_ftq_ids
        if missing_mandatory_ftq or missing_mandatory_mcq:
            self.missing_mandatory = list(missing_mandatory_mcq) + list(missing_mandatory_ftq)
            logger.warning(
                "questionnaire_missing_mandatory_questions",
                submission_id=str(self.submission.id),
                missing_count=len(self.missing_mandatory),
            )

    def _evaluate_mc_answers(self) -> None:
        """Scores all applicable multiple-choice answers in the submission."""
        # Only count max points for applicable questions
        self.max_mc_points = sum(
            q.positive_weight
            for q in self.questionnaire.multiplechoicequestion_questions.all()
            if q.id in self._applicable_mcq_ids
        ) or Decimal("0.0")

        # If we already know the submission fails on a mandatory check, don't bother scoring.
        if self.missing_mandatory:
            return

        for answer in self.submission.multiplechoiceanswer_answers.all().select_related("option", "question"):
            # Only score applicable questions
            if answer.question_id not in self._applicable_mcq_ids:
                continue
            if answer.option.is_correct:
                self.mc_points_scored += answer.question.positive_weight
            else:
                self.mc_points_scored -= answer.question.negative_weight
                self._fatalize(answer.question)

    def _evaluate_ft_answers_in_batch(self) -> None:
        """Evaluates all applicable free-text answers in a single batch call."""
        # Only evaluate answers for applicable questions
        answers_to_evaluate = [
            answer
            for answer in self.submission.freetextanswer_answers.all().select_related("question")
            if answer.question_id in self._applicable_ftq_ids
        ]
        if not answers_to_evaluate:
            return

        # Only count max points for applicable questions
        self.max_ft_points = sum(
            q.positive_weight
            for q in self.questionnaire.freetextquestion_questions.all()
            if q.id in self._applicable_ftq_ids
        ) or Decimal("0.0")

        # If we already know the submission fails on a mandatory check, don't waste tokens.
        if self._missing_mandatory_or_fatal():
            return

        questions_for_llm = [
            AnswerToEvaluate(
                question_id=answer.question_id,
                question_text=answer.question.question,
                answer_text=answer.answer,
                guidelines=answer.question.llm_guidelines,
            )
            for answer in answers_to_evaluate
        ]

        logger.info(
            "questionnaire_llm_evaluation_started",
            submission_id=str(self.submission.id),
            question_count=len(questions_for_llm),
            llm_backend=self.llm_evaluator.__class__.__name__,
        )
        self.llm_batch_response = self.llm_evaluator.evaluate(
            questions_to_evaluate=questions_for_llm,
            questionnaire_guidelines=self.questionnaire.llm_guidelines,
        )
        logger.info(
            "questionnaire_llm_evaluation_completed",
            submission_id=str(self.submission.id),
        )

        results_map = {result.question_id: result for result in self.llm_batch_response.evaluations}
        for answer in answers_to_evaluate:
            result = results_map.get(answer.question_id)
            if result:  # pragma: no branch
                if result.is_passing:
                    self.ft_points_scored += answer.question.positive_weight
                else:
                    self.ft_points_scored -= answer.question.negative_weight
                    self._fatalize(answer.question)

    def _missing_mandatory_or_fatal(self) -> bool:
        return bool(self.missing_mandatory) or self.fatal_error

    def _fatalize(self, question: FreeTextQuestion | MultipleChoiceQuestion) -> None:
        if question.is_fatal:
            self.fatal_error = True

    def _create_or_update_evaluation(self) -> QuestionnaireEvaluation:
        """Creates or updates the QuestionnaireEvaluation object.

        Applies the business logic for different evaluation modes and storing the audit trail.

        Returns:
            QuestionnaireEvaluation
        """
        total_points_scored = self.mc_points_scored + self.ft_points_scored
        total_max_points = self.max_mc_points + self.max_ft_points

        # Calculate final score and proposed pass/fail status
        if self.fatal_error or self.missing_mandatory:
            score_percent = Decimal("-100.0")
        else:
            score_percent = (
                ((total_points_scored / total_max_points) * Decimal("100.0"))
                if total_max_points > 0
                else Decimal("100.0")
            )

        passed = score_percent >= self.questionnaire.min_score
        proposed_status = (
            QuestionnaireEvaluation.QuestionnaireEvaluationProposedStatus.APPROVED
            if passed
            else QuestionnaireEvaluation.QuestionnaireEvaluationProposedStatus.REJECTED
        )

        # Build the structured audit data object
        # NEW: Pass the `missing_mandatory` flag to the audit data.
        audit_data = EvaluationAuditData(
            mc_points_scored=self.mc_points_scored,
            max_mc_points=self.max_mc_points,
            ft_points_scored=self.ft_points_scored,
            max_ft_points=self.max_ft_points,
            llm_response=self.llm_batch_response,
            missing_mandatory=self.missing_mandatory,  # <-- ADD THIS
        )

        # Prepare data for the database model
        evaluation_data = {
            "score": score_percent,
            "proposed_status": proposed_status,
            "raw_evaluation_data": audit_data.model_dump(mode="json"),
            "automatically_evaluated": True,
        }

        # Apply logic for different evaluation modes
        missing_mandatory_str = " One or more mandatory questions were not answered." if self.missing_mandatory else ""
        if self.questionnaire.evaluation_mode == Questionnaire.QuestionnaireEvaluationMode.AUTOMATIC:
            evaluation_data["status"] = proposed_status
            evaluation_data["comments"] = (
                f"This submission was automatically evaluated and finalized.{missing_mandatory_str}"
            )

        else:  # HYBRID or MANUAL
            evaluation_data["status"] = QuestionnaireEvaluation.QuestionnaireEvaluationStatus.PENDING_REVIEW
            evaluation_data["comments"] = (
                f"This submission was automatically evaluated and is pending human review.{missing_mandatory_str}"
            )

        evaluation, _ = QuestionnaireEvaluation.objects.select_related("submission").update_or_create(
            submission=self.submission,
            defaults=evaluation_data,
        )
        return evaluation
