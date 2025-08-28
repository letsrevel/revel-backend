import importlib
import typing as t
from decimal import Decimal
from functools import cached_property
from uuid import UUID

from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.db.models import Prefetch
from django.utils import timezone
from pydantic import BaseModel as PydanticBaseModel
from pydantic import Field as PydanticField

from common.models import TimeStampedModel
from questionnaires.llms.llm_interfaces import EvaluationResponse

from . import exceptions
from .llms.llm_interfaces import FreeTextEvaluator

# ---- Questionnaire model ----


class QuestionnaireQueryset(models.QuerySet["Questionnaire"]):
    """Questionnaire queryset."""

    def with_questions(self) -> t.Self:
        """With questions."""
        return self.prefetch_related(
            Prefetch(
                "multiplechoicequestion_questions", queryset=MultipleChoiceQuestion.objects.prefetch_related("options")
            ),
            "freetextquestion_questions",
        )


class QuestionnaireManager(models.Manager["Questionnaire"]):
    def get_queryset(self) -> QuestionnaireQueryset:
        """Get questionnaire queryset."""
        return QuestionnaireQueryset(self.model)

    def with_questions(self) -> QuestionnaireQueryset:
        """With questions."""
        return self.get_queryset().with_questions()


class Questionnaire(TimeStampedModel):
    class Status(models.TextChoices):
        DRAFT = "draft"
        READY = "ready"
        PUBLISHED = "published"

    class EvaluationMode(models.TextChoices):
        AUTOMATIC = "automatic"
        MANUAL = "manual"
        HYBRID = "hybrid"  # human-in-the-loop

    class LLMBackend(models.TextChoices):
        MOCK = "questionnaires.llms.MockEvaluator", "Mock Evaluator"
        VULNERABLE = "questionnaires.llms.VulnerableChatGPTEvaluator", "Vulnerable ChatGPTEvaluator"
        INTERMEDIATE = "questionnaires.llms.IntermediateChatGPTEvaluator", "Intermediate ChatGPTEvaluator"
        BETTER = "questionnaires.llms.BetterChatGPTEvaluator", "Better ChatGPTEvaluator"
        SANITIZING = "questionnaires.llms.SanitizingChatGPTEvaluator", "Sanitizing ChatGPTEvaluator"
        SENTINEL = "questionnaires.llms.SentinelChatGPTEvaluator", "Sentinel ChatGPTEvaluator"

    name = models.CharField(max_length=255, db_index=True)
    min_score = models.DecimalField(
        decimal_places=2, max_digits=5, default=0, validators=[MinValueValidator(0), MaxValueValidator(100)]
    )
    llm_guidelines = models.TextField(
        null=True,
        blank=True,
        help_text="LLM guidelines to evaluate automatically text-based answers. Can be overridden ad question-level.",
    )
    llm_backend = models.CharField(choices=LLMBackend.choices, max_length=255, default=LLMBackend.MOCK)
    shuffle_questions = models.BooleanField(default=False, help_text="Shuffle questions before answering.")
    shuffle_sections = models.BooleanField(default=False, help_text="Shuffle sections before answering.")
    status = models.CharField(choices=Status.choices, max_length=10, default=Status.DRAFT, db_index=True)
    evaluation_mode = models.CharField(choices=EvaluationMode.choices, max_length=20, default=EvaluationMode.AUTOMATIC)
    can_retake_after = models.DurationField(null=True, blank=True, help_text="How long to wait to be able to retake.")
    max_attempts = models.IntegerField(default=0, help_text="Max number of attempts to answer. 0 means unlimited.")

    objects = QuestionnaireManager()

    def get_llm_backend(self) -> FreeTextEvaluator:
        """Get the LLM backend."""
        module_path, _, class_name = self.llm_backend.rpartition(".")
        if not module_path:
            raise ImportError(f"No module part in '{self.llm_backend}'")
        module = importlib.import_module(module_path)
        return t.cast(FreeTextEvaluator, getattr(module, class_name)())


# ---- QuestionnaireSubmission ----


class QuestionnaireSubmissionQueryset(models.QuerySet["QuestionnaireSubmission"]):
    """QuestionnaireSubmission queryset."""

    def ready(self) -> t.Self:
        """Filter for ready submissions."""
        return self.filter(status="ready").order_by("-submitted_at")


class QuestionnaireSubmissionManager(models.Manager["QuestionnaireSubmission"]):
    def get_queryset(self) -> QuestionnaireSubmissionQueryset:
        """Get QuestionnaireSubmission queryset."""
        return QuestionnaireSubmissionQueryset(self.model)

    def ready(self) -> models.QuerySet["QuestionnaireSubmission"]:
        """Filter for ready submissions."""
        return self.get_queryset().ready()


class QuestionnaireSubmission(TimeStampedModel):
    class Status(models.TextChoices):
        DRAFT = "draft"
        READY = "ready"

    questionnaire = models.ForeignKey(Questionnaire, on_delete=models.CASCADE, related_name="questionnaire_submissions")
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="questionnaire_submissions"
    )
    status = models.CharField(
        choices=Status.choices,
        max_length=10,
        default=Status.DRAFT,
        help_text="The status of the submission.",
        db_index=True,
    )
    submitted_at = models.DateTimeField(db_index=True, null=True, blank=True)

    submission_count: int

    objects = QuestionnaireSubmissionManager()

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["user", "questionnaire"],
                condition=models.Q(status="draft"),
                name="unique_draft_submission_per_user",
            )
        ]
        ordering = ["-submitted_at"]

    def clean(self) -> None:
        """Set submitted_at when status is changed to SUBMITTED."""
        super().clean()
        if self.status == self.Status.READY and not self.submitted_at:
            # We could also use django.utils.timezone.now here
            self.submitted_at = timezone.now()


# ---- QuestionnaireSection model ----


class QuestionnaireSectionQueryset(models.QuerySet["QuestionnaireSection"]):
    """Questionnaire section queryset."""


class QuestionnaireSectionManager(models.Manager["QuestionnaireSection"]):
    """Questionnaire section manager."""

    def get_queryset(self) -> QuestionnaireSectionQueryset:
        """Get questionnaire section queryset."""
        return QuestionnaireSectionQueryset(self.model)


class QuestionnaireSection(TimeStampedModel):
    questionnaire = models.ForeignKey(Questionnaire, on_delete=models.CASCADE, related_name="sections")
    name = models.CharField(max_length=255)
    order = models.PositiveIntegerField(default=0)

    objects = QuestionnaireSectionManager()

    class Meta:
        ordering = ["order"]


# ---- Abstract Base Models ----


class BaseQuestion(TimeStampedModel):
    """An abstract model for a question in a questionnaire."""

    questionnaire = models.ForeignKey(Questionnaire, on_delete=models.CASCADE, related_name="%(class)s_questions")
    section = models.ForeignKey(
        QuestionnaireSection, on_delete=models.CASCADE, related_name="%(class)s_questions", null=True, blank=True
    )
    question = models.TextField()
    positive_weight = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=Decimal("1.0"),
        validators=[MinValueValidator(0), MaxValueValidator(100)],
        help_text="Points scored when answered correctly.",
    )
    negative_weight = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=Decimal("0.0"),
        validators=[MinValueValidator(-100), MaxValueValidator(100)],
        help_text="Points deducted when answered incorrectly.",
    )
    is_fatal = models.BooleanField(default=False, help_text="A fatal question will fail the questionnaire.")
    is_mandatory = models.BooleanField(default=False)
    order = models.PositiveIntegerField(
        default=0,
        db_index=True,
        help_text="The order the questions are displayed. Ignored if questionnaire.shuffle_questions is True.",
    )

    def clean(self) -> None:
        """Ensure section's questionnaire and question's questionnaire match."""
        super().clean()
        if self.section and self.section.questionnaire != self.questionnaire:
            raise exceptions.CrossQuestionnaireSectionError(
                {"section": "The selected section does not belong to the question's questionnaire."}
            )

    class Meta:
        abstract = True
        ordering = ["order"]


class BaseAnswer(TimeStampedModel):
    """An abstract model for a user's answer to a question."""

    submission = models.ForeignKey(QuestionnaireSubmission, on_delete=models.CASCADE, related_name="%(class)s_answers")

    class Meta:
        abstract = True


# ---- Concrete models ----


# ---- MultipleChoiceQuestion ----


class MultipleChoiceQuestionQueryset(models.QuerySet["MultipleChoiceQuestion"]):
    """Questionnaire queryset."""


class MultipleChoiceQuestionManager(models.Manager["MultipleChoiceQuestion"]):
    def get_queryset(self) -> MultipleChoiceQuestionQueryset:
        """Get MultipleChoiceQuestion queryset."""
        return MultipleChoiceQuestionQueryset(self.model)


class MultipleChoiceQuestion(BaseQuestion):
    allow_multiple_answers = models.BooleanField(default=False)
    shuffle_options = models.BooleanField(
        default=True, help_text="Shuffle the order the options are displayed each time."
    )

    objects = MultipleChoiceQuestionManager()


# ---- MultipleChoiceOption ----


class MultipleChoiceOptionQueryset(models.QuerySet["MultipleChoiceOption"]):
    """MultipleChoiceOption queryset."""


class MultipleChoiceOptionManager(models.Manager["MultipleChoiceOption"]):
    def get_queryset(self) -> MultipleChoiceOptionQueryset:
        """Get MultipleChoiceOption queryset."""
        return MultipleChoiceOptionQueryset(self.model)


class MultipleChoiceOption(TimeStampedModel):
    question = models.ForeignKey(MultipleChoiceQuestion, on_delete=models.CASCADE, related_name="options")
    option = models.CharField(max_length=255)
    is_correct = models.BooleanField(default=False)
    order = models.PositiveIntegerField(
        default=0,
        db_index=True,
        help_text="The order the options are displayed. Ignored if question.shuffle_options is True.",
    )

    objects = MultipleChoiceOptionManager()

    def clean(self) -> None:
        """Ensure that for single-answer questions, only one option can be marked as correct."""
        super().clean()

        if self.is_correct and not self.question.allow_multiple_answers:
            other_correct_options = MultipleChoiceOption.objects.filter(
                question=self.question, is_correct=True
            ).exclude(pk=self.pk)

            if other_correct_options.exists():
                raise exceptions.MultipleCorrectOptionsError(
                    {
                        "is_correct": "This question does not allow multiple correct answers. "
                        "Another option is already marked as correct."
                    }
                )


# ---- MultipleChoiceAnswer ----


class MultipleChoiceAnswerQueryset(models.QuerySet["MultipleChoiceAnswer"]):
    """MultipleChoiceAnswer queryset."""


class MultipleChoiceAnswerManager(models.Manager["MultipleChoiceAnswer"]):
    def get_queryset(self) -> MultipleChoiceAnswerQueryset:
        """Get MultipleChoiceAnswer queryset."""
        return MultipleChoiceAnswerQueryset(self.model)


class MultipleChoiceAnswer(BaseAnswer):
    question = models.ForeignKey(MultipleChoiceQuestion, on_delete=models.CASCADE, related_name="answers")
    option = models.ForeignKey(MultipleChoiceOption, on_delete=models.CASCADE, related_name="answers")

    objects = MultipleChoiceAnswerManager()

    def clean(self) -> None:
        """Ensure that a user cannot submit multiple answers to a question that does not allow it."""
        super().clean()
        if self.question.allow_multiple_answers is False:  # pragma: no branch
            # Check for other answers belonging to the SAME SUBMISSION and the SAME QUESTION.
            query = MultipleChoiceAnswer.objects.filter(submission=self.submission, question=self.question)
            if self.pk:
                query = query.exclude(pk=self.pk)
            if query.exists():
                raise exceptions.DisallowedMultipleAnswersError(
                    {"question": "Multiple answers are not allowed for this question."}
                )

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["submission", "question", "option"], name="unique_user_question_option"),
        ]
        indexes = [models.Index(fields=["submission", "question"], name="mca_user_question_idx")]


# ---- FreeTextQuestion ----


class FreeTextQuestionQueryset(models.QuerySet["FreeTextQuestion"]):
    """FreeTextQuestion queryset."""


class FreeTextQuestionManager(models.Manager["FreeTextQuestion"]):
    def get_queryset(self) -> FreeTextQuestionQueryset:
        """Get FreeTextQuestion queryset."""
        return FreeTextQuestionQueryset(self.model)


class FreeTextQuestion(BaseQuestion):
    llm_guidelines = models.TextField(
        null=True,
        blank=True,
        help_text="LLM guidelines to evaluate automatically text-based answers. "
        "If provided, adds to the questionnaire.llm_guidelines specifically for the question.",
    )

    objects = FreeTextQuestionManager()


# ---- FreeTextAnswer ----


class FreeTextAnswerQueryset(models.QuerySet["FreeTextAnswer"]):
    """FreeTextAnswer queryset."""


class FreeTextAnswerManager(models.Manager["FreeTextAnswer"]):
    def get_queryset(self) -> FreeTextAnswerQueryset:
        """Get FreeTextAnswer queryset."""
        return FreeTextAnswerQueryset(self.model)


class FreeTextAnswer(BaseAnswer):
    question = models.ForeignKey(FreeTextQuestion, on_delete=models.CASCADE, related_name="answers")
    answer = models.TextField()

    objects = FreeTextAnswerManager()

    class Meta:
        constraints = [models.UniqueConstraint(fields=["submission", "question"], name="unique_user_freetext_answer")]
        indexes = [models.Index(fields=["submission", "question"], name="fta_user_question_idx")]


# ---- QuestionnaireEvaluation ----


class QuestionnaireEvaluationQueryset(models.QuerySet["QuestionnaireEvaluation"]):
    """QuestionnaireEvaluation queryset."""


class QuestionnaireEvaluationManager(models.Manager["QuestionnaireEvaluation"]):
    def get_queryset(self) -> QuestionnaireEvaluationQueryset:
        """Get QuestionnaireEvaluation queryset."""
        return QuestionnaireEvaluationQueryset(self.model)


class EvaluationAuditData(PydanticBaseModel):
    """A structured Pydantic model for storing the complete audit trail."""

    mc_points_scored: Decimal
    max_mc_points: Decimal
    ft_points_scored: Decimal
    max_ft_points: Decimal
    missing_mandatory: list[UUID] | None = PydanticField(None, description="The IDs of missing mandatory answers.")
    llm_response: EvaluationResponse | None = PydanticField(
        None, description="The complete, raw response from the LLM batch evaluation."
    )


class QuestionnaireEvaluation(TimeStampedModel):
    class Status(models.TextChoices):
        APPROVED = "approved"
        REJECTED = "rejected"
        PENDING_REVIEW = "pending review"

    class ProposedStatus(models.TextChoices):
        APPROVED = "approved"
        REJECTED = "rejected"

    submission = models.OneToOneField(QuestionnaireSubmission, on_delete=models.CASCADE, related_name="evaluation")
    score = models.DecimalField(decimal_places=2, max_digits=10, null=True, blank=True)
    raw_evaluation_data = models.JSONField(null=True, blank=True)
    status = models.CharField(choices=Status.choices, max_length=20, default=Status.PENDING_REVIEW)
    proposed_status = models.CharField(
        null=True, default=None, choices=ProposedStatus.choices, max_length=20, db_index=True, blank=True
    )
    comments = models.TextField(null=True, blank=True)
    automatically_evaluated = models.BooleanField(default=False)
    evaluator = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)

    objects = QuestionnaireEvaluationManager()

    @cached_property
    def evaluation_data(self) -> EvaluationAuditData:
        """Parse raw_evaluation_data EvaluationResponse."""
        return EvaluationAuditData.model_validate(self.raw_evaluation_data)

    class Meta:
        ordering = ["-updated_at"]
