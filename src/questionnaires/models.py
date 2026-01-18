import importlib
import os
import typing as t
import uuid
from decimal import Decimal
from functools import cached_property
from pathlib import Path

from django.conf import settings
from django.contrib.postgres.fields import ArrayField
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.db.models import Prefetch
from django.utils import timezone
from pydantic import BaseModel as PydanticBaseModel
from pydantic import Field as PydanticField

from common.fields import MarkdownField, ProtectedFileField
from common.models import TimeStampedModel
from questionnaires.llms.llm_interfaces import EvaluationResponse

from . import exceptions
from .llms.llm_interfaces import FreeTextEvaluator

# ---- QuestionnaireFile model ----


def questionnaire_file_upload_path(instance: "QuestionnaireFile", filename: str) -> str:
    """Generate UUID-based path to prevent enumeration.

    Path structure: questionnaire_files/{user_id}/{uuid}.{ext}

    This structure:
    - Uses UUIDs to prevent enumeration attacks
    - Is scoped per user for organization
    - ProtectedFileField adds the 'protected/' prefix automatically

    Note: The 'protected/' prefix is added by ProtectedFileField, so this
    function returns paths without it.
    """
    ext = Path(filename).suffix[:10]  # Limit extension length for safety
    return f"questionnaire_files/{instance.uploader_id}/{uuid.uuid4()}{ext}"


class QuestionnaireFileQueryset(models.QuerySet["QuestionnaireFile"]):
    """QuestionnaireFile queryset."""

    def for_user(self, user: "settings.AUTH_USER_MODEL") -> t.Self:  # type: ignore[name-defined]
        """Filter files by uploader."""
        return self.filter(uploader=user)


class QuestionnaireFileManager(models.Manager["QuestionnaireFile"]):
    """QuestionnaireFile manager."""

    def get_queryset(self) -> QuestionnaireFileQueryset:
        """Get QuestionnaireFile queryset."""
        return QuestionnaireFileQueryset(self.model, using=self._db)

    def for_user(self, user: "settings.AUTH_USER_MODEL") -> QuestionnaireFileQueryset:  # type: ignore[name-defined]
        """Filter files by uploader."""
        return self.get_queryset().for_user(user)


class QuestionnaireFile(TimeStampedModel):
    """A file uploaded by a user for use in questionnaire answers.

    Files are stored in a user-scoped library and can be reused across multiple
    questions/questionnaires via M2M relationship with FileUploadAnswer.

    Deletion Policy (Privacy First):
        When a user deletes a file, it is HARD DELETED from storage immediately,
        even if referenced by submitted questionnaires. User privacy takes precedence
        over data integrity. Submissions with deleted files will show the file as
        unavailable. This is intentional for GDPR/privacy compliance.
    """

    uploader = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="questionnaire_files",
    )
    file = ProtectedFileField(
        upload_to=questionnaire_file_upload_path,
        max_length=255,  # Accommodate long paths with UUIDs
    )
    original_filename = models.CharField(max_length=255)
    file_hash = models.CharField(
        max_length=64,
        db_index=True,
        help_text="SHA-256 hash of file content for deduplication.",
    )
    mime_type = models.CharField(max_length=100)
    file_size = models.PositiveIntegerField(help_text="File size in bytes.")

    objects = QuestionnaireFileManager()

    class Meta:
        constraints = [
            # Prevent duplicate uploads per user (same content = same hash)
            models.UniqueConstraint(
                fields=["uploader", "file_hash"],
                name="unique_questionnaire_file_per_user",
            )
        ]
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.original_filename} ({self.uploader})"

    def save(self, *args: t.Any, **kwargs: t.Any) -> None:
        """Truncate original_filename if needed, preserving extension."""
        if self.original_filename and len(self.original_filename) > 255:
            name, ext = os.path.splitext(self.original_filename)
            max_name_len = 255 - len(ext) - 3  # Reserve space for "..."
            self.original_filename = f"{name[:max_name_len]}...{ext}"
        super().save(*args, **kwargs)

    def delete(self, *args: t.Any, **kwargs: t.Any) -> tuple[int, dict[str, int]]:
        """Delete file from storage when model is deleted.

        Privacy Policy: Files are HARD DELETED immediately, including from storage.
        This applies even if the file was used in submitted questionnaires - user
        privacy takes precedence over data integrity.
        """
        from django.db import transaction

        with transaction.atomic():
            # Clear M2M relationships first (not strictly necessary as Django handles it,
            # but explicit is better for understanding the deletion cascade)
            self.file_upload_answers.clear()
            # Delete file from storage
            if self.file:
                self.file.delete(save=False)
            return super().delete(*args, **kwargs)


class SubmissionSourceEventMetadata(t.TypedDict):
    """Metadata about the event context when a questionnaire was submitted."""

    event_id: str
    event_name: str
    event_start: str  # ISO 8601 datetime string


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
            "fileuploadquestion_questions",
        )


class QuestionnaireManager(models.Manager["Questionnaire"]):
    def get_queryset(self) -> QuestionnaireQueryset:
        """Get questionnaire queryset."""
        return QuestionnaireQueryset(self.model)

    def with_questions(self) -> QuestionnaireQueryset:
        """With questions."""
        return self.get_queryset().with_questions()


class Questionnaire(TimeStampedModel):
    class QuestionnaireStatus(models.TextChoices):
        DRAFT = "draft"
        READY = "ready"
        PUBLISHED = "published"

    class QuestionnaireEvaluationMode(models.TextChoices):
        AUTOMATIC = "automatic"
        MANUAL = "manual"
        HYBRID = "hybrid"  # human-in-the-loop

    class QuestionnaireLLMBackend(models.TextChoices):
        MOCK = "questionnaires.llms.MockEvaluator", "Mock Evaluator"
        VULNERABLE = "questionnaires.llms.VulnerableChatGPTEvaluator", "Vulnerable ChatGPTEvaluator"
        INTERMEDIATE = "questionnaires.llms.IntermediateChatGPTEvaluator", "Intermediate ChatGPTEvaluator"
        BETTER = "questionnaires.llms.BetterChatGPTEvaluator", "Better ChatGPTEvaluator"
        SANITIZING = "questionnaires.llms.SanitizingChatGPTEvaluator", "Sanitizing ChatGPTEvaluator"
        SENTINEL = "questionnaires.llms.SentinelChatGPTEvaluator", "Sentinel ChatGPTEvaluator"

    name = models.CharField(max_length=255, db_index=True)
    description = MarkdownField(
        null=True,
        blank=True,
        help_text="Markdown-formatted description shown to users before they start the questionnaire.",
    )
    min_score = models.DecimalField(
        decimal_places=2, max_digits=5, default=0, validators=[MinValueValidator(0), MaxValueValidator(100)]
    )
    llm_guidelines = models.TextField(
        null=True,
        blank=True,
        help_text="LLM guidelines to evaluate automatically text-based answers. Can be overridden ad question-level.",
    )
    llm_backend = models.CharField(
        choices=QuestionnaireLLMBackend.choices, max_length=255, default=QuestionnaireLLMBackend.SANITIZING
    )
    shuffle_questions = models.BooleanField(default=False, help_text="Shuffle questions before answering.")
    shuffle_sections = models.BooleanField(default=False, help_text="Shuffle sections before answering.")
    status = models.CharField(
        choices=QuestionnaireStatus.choices, max_length=10, default=QuestionnaireStatus.DRAFT, db_index=True
    )
    evaluation_mode = models.CharField(
        choices=QuestionnaireEvaluationMode.choices, max_length=20, default=QuestionnaireEvaluationMode.AUTOMATIC
    )
    can_retake_after = models.DurationField(null=True, blank=True, help_text="How long to wait to be able to retake.")
    max_attempts = models.IntegerField(default=0, help_text="Max number of attempts to answer. 0 means unlimited.")

    objects = QuestionnaireManager()

    def get_llm_backend(self) -> FreeTextEvaluator:
        """Get the LLM backend."""
        backend = self.llm_backend
        if settings.DEMO_MODE:
            backend = self.QuestionnaireLLMBackend.MOCK

        module_path, _, class_name = backend.rpartition(".")
        if not module_path:
            raise ImportError(f"No module part in '{self.llm_backend}'")
        module = importlib.import_module(module_path)
        try:
            return t.cast(FreeTextEvaluator, getattr(module, class_name)())
        except AttributeError as e:
            if class_name == "SentinelChatGPTEvaluator":
                msg = (
                    f"The {class_name} backend requires the 'transformers' library which is not installed. "
                    "Please install it with: uv sync --group sentinel"
                )
                raise ImportError(msg) from e
            raise


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
    class QuestionnaireSubmissionStatus(models.TextChoices):
        DRAFT = "draft"
        READY = "ready"

    questionnaire = models.ForeignKey(Questionnaire, on_delete=models.CASCADE, related_name="questionnaire_submissions")
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="questionnaire_submissions"
    )
    status = models.CharField(
        choices=QuestionnaireSubmissionStatus.choices,
        max_length=10,
        default=QuestionnaireSubmissionStatus.DRAFT,
        help_text="The status of the submission.",
        db_index=True,
    )
    submitted_at = models.DateTimeField(db_index=True, null=True, blank=True)
    metadata = models.JSONField(
        null=True,
        blank=True,
        help_text="Optional metadata about the submission context (e.g., source event).",
    )

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
        if self.status == self.QuestionnaireSubmissionStatus.READY and not self.submitted_at:
            # We could also use django.utils.timezone.now here
            self.submitted_at = timezone.now()

    @property
    def source_event(self) -> SubmissionSourceEventMetadata | None:
        """Get the source event metadata if available."""
        if self.metadata and "source_event" in self.metadata:
            return t.cast(SubmissionSourceEventMetadata, self.metadata["source_event"])
        return None


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
    description = MarkdownField(
        null=True,
        blank=True,
        help_text="Markdown-formatted description shown at the top of this section.",
    )
    order = models.PositiveIntegerField(default=0)
    depends_on_option = models.ForeignKey(
        "MultipleChoiceOption",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="dependent_sections",
        help_text="Section is only shown if this option was selected.",
    )

    objects = QuestionnaireSectionManager()

    class Meta:
        ordering = ["order"]

    def clean(self) -> None:
        """Validate that depends_on_option belongs to the same questionnaire."""
        super().clean()
        if self.depends_on_option:
            if self.depends_on_option.question.questionnaire_id != self.questionnaire_id:
                raise exceptions.CrossQuestionnaireOptionDependencyError(
                    {"depends_on_option": "The selected option does not belong to this questionnaire."}
                )


# ---- Abstract Base Models ----


class InformationalQuestionMixin(models.Model):
    """Mixin for questions that are informational only (no scoring by default).

    This mixin overrides positive_weight to default to 0.0 instead of 1.0,
    since these question types (e.g., file uploads) cannot be automatically
    evaluated and are treated as informational/supplementary by default.
    """

    positive_weight = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=Decimal("0.0"),
        validators=[MinValueValidator(0), MaxValueValidator(100)],
        help_text="Points scored when answered correctly. Defaults to 0 for informational questions.",
    )

    class Meta:
        abstract = True


class BaseQuestion(TimeStampedModel):
    """An abstract model for a question in a questionnaire."""

    questionnaire = models.ForeignKey(Questionnaire, on_delete=models.CASCADE, related_name="%(class)s_questions")
    section = models.ForeignKey(
        QuestionnaireSection, on_delete=models.CASCADE, related_name="%(class)s_questions", null=True, blank=True
    )
    question = MarkdownField(help_text="The question text. Supports markdown formatting.")
    hint = MarkdownField(
        null=True,
        blank=True,
        help_text="Markdown-formatted hint or additional context shown to the user below the question.",
    )
    reviewer_notes = MarkdownField(
        null=True,
        blank=True,
        help_text="Markdown-formatted notes for reviewers. Not shown to users.",
    )
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
    depends_on_option = models.ForeignKey(
        "MultipleChoiceOption",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="dependent_%(class)s_questions",
        help_text="Question is only shown if this option was selected.",
    )

    def clean(self) -> None:
        """Validate section and depends_on_option constraints."""
        super().clean()
        if self.section and self.section.questionnaire != self.questionnaire:
            raise exceptions.CrossQuestionnaireSectionError(
                {"section": "The selected section does not belong to the question's questionnaire."}
            )
        if self.depends_on_option:
            if self.depends_on_option.question.questionnaire_id != self.questionnaire_id:
                raise exceptions.CrossQuestionnaireOptionDependencyError(
                    {"depends_on_option": "The selected option does not belong to this questionnaire."}
                )
            # Ensure the dependency is on a question with lower order
            # if self.depends_on_option.question.order >= self.order:
            #     raise exceptions.InvalidOptionDependencyOrderError(
            #         {"depends_on_option": "Cannot depend on an option from a question with equal or higher order."}
            #     )

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


# ---- FileUploadQuestion ----


class FileUploadQuestionQueryset(models.QuerySet["FileUploadQuestion"]):
    """FileUploadQuestion queryset."""


class FileUploadQuestionManager(models.Manager["FileUploadQuestion"]):
    def get_queryset(self) -> FileUploadQuestionQueryset:
        """Get FileUploadQuestion queryset."""
        return FileUploadQuestionQueryset(self.model)


class FileUploadQuestion(InformationalQuestionMixin, BaseQuestion):
    """A question that accepts file/image uploads as answers.

    This question type is treated as informational by default (positive_weight=0.0)
    since automatic LLM evaluation of files is not yet implemented.
    Evaluators can manually review uploaded files in the submission detail view.

    Note: The InformationalQuestionMixin overrides the default positive_weight from 1.0
    to 0.0, ensuring consistency between ORM creation and API schema defaults.
    """

    allowed_mime_types = ArrayField(
        models.CharField(max_length=100),
        default=list,
        blank=True,
        help_text="Allowed MIME types (e.g., 'image/jpeg', 'application/pdf'). Empty = allow all.",
    )
    max_file_size = models.PositiveIntegerField(
        default=5 * 1024 * 1024,  # 5MB
        help_text="Maximum file size in bytes per file.",
    )
    max_files = models.PositiveIntegerField(
        default=1,
        validators=[MinValueValidator(1), MaxValueValidator(10)],
        help_text="Maximum number of files allowed for this question.",
    )

    objects = FileUploadQuestionManager()


# ---- FileUploadAnswer ----


class FileUploadAnswerQueryset(models.QuerySet["FileUploadAnswer"]):
    """FileUploadAnswer queryset."""


class FileUploadAnswerManager(models.Manager["FileUploadAnswer"]):
    def get_queryset(self) -> FileUploadAnswerQueryset:
        """Get FileUploadAnswer queryset."""
        return FileUploadAnswerQueryset(self.model)


class FileUploadAnswer(BaseAnswer):
    """A user's file upload answer to a FileUploadQuestion.

    Uses M2M relationship to QuestionnaireFile to allow:
    - Reusing the same file across multiple questions/questionnaires
    - Multiple files per answer (up to question.max_files)
    """

    question = models.ForeignKey(
        FileUploadQuestion,
        on_delete=models.CASCADE,
        related_name="answers",
    )
    files = models.ManyToManyField(
        QuestionnaireFile,
        related_name="file_upload_answers",
    )

    objects = FileUploadAnswerManager()

    def __str__(self) -> str:
        return f"FileUploadAnswer for Q{self.question_id} in submission {self.submission_id}"

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["submission", "question"],
                name="unique_user_fileupload_answer",
            )
        ]
        indexes = [models.Index(fields=["submission", "question"], name="fua_user_question_idx")]


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
    missing_mandatory: list[uuid.UUID] | None = PydanticField(None, description="The IDs of missing mandatory answers.")
    llm_response: EvaluationResponse | None = PydanticField(
        None, description="The complete, raw response from the LLM batch evaluation."
    )


class QuestionnaireEvaluation(TimeStampedModel):
    class QuestionnaireEvaluationStatus(models.TextChoices):
        APPROVED = "approved"
        REJECTED = "rejected"
        PENDING_REVIEW = "pending review"

    class QuestionnaireEvaluationProposedStatus(models.TextChoices):
        APPROVED = "approved"
        REJECTED = "rejected"

    submission = models.OneToOneField(QuestionnaireSubmission, on_delete=models.CASCADE, related_name="evaluation")
    score = models.DecimalField(decimal_places=2, max_digits=10, null=True, blank=True)
    raw_evaluation_data = models.JSONField(null=True, blank=True)
    status = models.CharField(
        choices=QuestionnaireEvaluationStatus.choices,
        max_length=20,
        default=QuestionnaireEvaluationStatus.PENDING_REVIEW,
    )
    proposed_status = models.CharField(
        null=True,
        default=None,
        choices=QuestionnaireEvaluationProposedStatus.choices,
        max_length=20,
        db_index=True,
        blank=True,
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
