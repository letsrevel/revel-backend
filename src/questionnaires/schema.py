import typing as t
from datetime import datetime, timedelta
from decimal import Decimal
from uuid import UUID

from ninja import ModelSchema, Schema
from pydantic import Field, field_serializer, field_validator, model_validator
from pydantic_core import PydanticCustomError

from accounts.schema import MinimalRevelUserSchema
from common.signing import get_file_url
from questionnaires.models import (
    Questionnaire,
    QuestionnaireEvaluation,
    QuestionnaireFile,
    QuestionnaireSubmission,
)

# ---- MIME type constants for file upload validation ----

# Common document MIME types
DOCUMENT_MIME_TYPES: frozenset[str] = frozenset(
    {
        "application/pdf",
        "application/msword",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",  # .docx
        "application/vnd.ms-excel",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",  # .xlsx
        "application/vnd.ms-powerpoint",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",  # .pptx
        "text/plain",
        "text/csv",
        "application/rtf",
    }
)

# Common image MIME types
# NOTE: SVG excluded intentionally - can contain embedded JavaScript (XSS risk)
IMAGE_MIME_TYPES: frozenset[str] = frozenset(
    {
        "image/jpeg",
        "image/png",
        "image/gif",
        "image/webp",
        "image/bmp",
        "image/tiff",
    }
)

# Common audio MIME types
AUDIO_MIME_TYPES: frozenset[str] = frozenset(
    {
        "audio/mpeg",  # .mp3
        "audio/wav",
        "audio/ogg",
        "audio/webm",
        "audio/aac",
        "audio/flac",
    }
)

# Common video MIME types
VIDEO_MIME_TYPES: frozenset[str] = frozenset(
    {
        "video/mp4",
        "video/webm",
        "video/ogg",
        "video/quicktime",  # .mov
        "video/x-msvideo",  # .avi
    }
)

# Archive MIME types
ARCHIVE_MIME_TYPES: frozenset[str] = frozenset(
    {
        "application/zip",
        "application/x-rar-compressed",
        "application/x-7z-compressed",
        "application/gzip",
        "application/x-tar",
    }
)

# All allowed MIME types for questionnaire file uploads
ALLOWED_QUESTIONNAIRE_MIME_TYPES: frozenset[str] = (
    DOCUMENT_MIME_TYPES | IMAGE_MIME_TYPES | AUDIO_MIME_TYPES | VIDEO_MIME_TYPES | ARCHIVE_MIME_TYPES
)

# Question type literal for type safety
QuestionType = t.Literal["multiple_choice", "free_text", "file_upload"]


def seconds_to_timedelta(v: timedelta | int | str | None) -> timedelta | None:
    """Convert seconds (int or string) to timedelta for storage."""
    if v is None:
        return None
    if isinstance(v, timedelta):
        return v
    if isinstance(v, str):
        v = int(v)
    return timedelta(seconds=v)


def timedelta_to_seconds(value: timedelta | int | None) -> int | None:
    """Convert timedelta to seconds for serialization."""
    if value is None:
        return None
    if isinstance(value, timedelta):
        return int(value.total_seconds())
    return value


class BaseUUIDSchema(Schema):
    id: UUID


# ---- QuestionnaireFile schemas (user's file library) ----


class QuestionnaireFileSchema(ModelSchema):
    """Schema for QuestionnaireFile in API responses."""

    id: UUID
    original_filename: str
    mime_type: str
    file_size: int
    file_url: str | None = None
    thumbnail_url: str | None = None
    preview_url: str | None = None

    @staticmethod
    def resolve_file_url(obj: QuestionnaireFile) -> str | None:
        """Resolve the file URL with signature for protected paths."""
        return get_file_url(obj.file)

    @staticmethod
    def resolve_thumbnail_url(obj: QuestionnaireFile) -> str | None:
        """Resolve thumbnail URL (signed for protected files)."""
        return get_file_url(obj.thumbnail)

    @staticmethod
    def resolve_preview_url(obj: QuestionnaireFile) -> str | None:
        """Resolve preview URL (signed for protected files)."""
        return get_file_url(obj.preview)

    class Meta:
        model = QuestionnaireFile
        fields = ["id", "original_filename", "mime_type", "file_size", "created_at"]


class BaseQuestionSchema(BaseUUIDSchema):
    question: str
    hint: str | None = None
    is_mandatory: bool
    order: int
    depends_on_option_id: UUID | None = None


class MultipleChoiceOptionSchema(BaseUUIDSchema):
    option: str
    order: int


class MultipleChoiceQuestionSchema(BaseQuestionSchema):
    allow_multiple_answers: bool
    options: list[MultipleChoiceOptionSchema]


class FreeTextQuestionSchema(BaseQuestionSchema):
    pass


class FileUploadQuestionSchema(BaseQuestionSchema):
    """Schema for file upload questions displayed to users filling out questionnaires."""

    allowed_mime_types: list[str]
    max_file_size: int
    max_files: int


class QuestionContainerSchema(BaseUUIDSchema):
    name: str
    description: str | None = None
    multiple_choice_questions: list[MultipleChoiceQuestionSchema] = Field(default_factory=list)
    free_text_questions: list[FreeTextQuestionSchema] = Field(default_factory=list)
    file_upload_questions: list[FileUploadQuestionSchema] = Field(default_factory=list)


class SectionSchema(QuestionContainerSchema):
    order: int
    depends_on_option_id: UUID | None = None


class QuestionnaireSchema(QuestionContainerSchema):
    sections: list[SectionSchema] = Field(default_factory=list)
    evaluation_mode: Questionnaire.QuestionnaireEvaluationMode


# --- Questionnaire Submission ---


class MultipleChoiceSubmissionSchema(Schema):
    question_id: UUID
    options_id: list[UUID]


class FreeTextSubmissionSchema(Schema):
    question_id: UUID
    answer: str = Field(..., min_length=1, max_length=500)


class FileUploadSubmissionSchema(Schema):
    """Schema for submitting file upload answers."""

    question_id: UUID
    file_ids: list[UUID] = Field(..., min_length=1)


class QuestionnaireSubmissionSchema(Schema):
    questionnaire_id: UUID
    multiple_choice_answers: list[MultipleChoiceSubmissionSchema] = Field(default_factory=list)
    free_text_answers: list[FreeTextSubmissionSchema] = Field(default_factory=list)
    file_upload_answers: list[FileUploadSubmissionSchema] = Field(default_factory=list)
    status: QuestionnaireSubmission.QuestionnaireSubmissionStatus

    @model_validator(mode="after")
    def ensure_unique_question_ids(self) -> "QuestionnaireSubmissionSchema":
        """A validator to ensure unique question ids are not repeated."""
        all_question_ids = (
            [mc.question_id for mc in self.multiple_choice_answers]
            + [ft.question_id for ft in self.free_text_answers]
            + [fu.question_id for fu in self.file_upload_answers]
        )

        duplicates = {qid for qid in all_question_ids if all_question_ids.count(qid) > 1}

        if duplicates:
            raise PydanticCustomError(
                "duplicate_question_ids",
                f"Each question must be answered only once."
                f" Duplicated question IDs: {sorted(str(d) for d in duplicates)}",
            )

        return self


class QuestionnaireSubmissionResponseSchema(ModelSchema):
    questionnaire_id: UUID
    status: QuestionnaireSubmission.QuestionnaireSubmissionStatus
    submitted_at: datetime

    class Meta:
        model = QuestionnaireSubmission
        fields = ["status", "submitted_at"]


class QuestionnaireEvaluationForUserSchema(ModelSchema):
    submission: QuestionnaireSubmissionResponseSchema
    score: Decimal
    status: QuestionnaireEvaluation.QuestionnaireEvaluationStatus

    class Meta:
        model = QuestionnaireEvaluation
        fields = ["submission", "score", "status"]


QuestionnaireSubmissionOrEvaluationSchema = QuestionnaireSubmissionResponseSchema | QuestionnaireEvaluationForUserSchema


# Submission management schemas for organization staff


class SubmissionListItemSchema(ModelSchema):
    """Schema for listing submissions for organization staff."""

    id: UUID
    user: MinimalRevelUserSchema
    questionnaire_name: str
    evaluation_status: QuestionnaireEvaluation.QuestionnaireEvaluationStatus | None = None
    evaluation_score: Decimal | None = None
    metadata: dict[str, t.Any] | None = None

    class Meta:
        model = QuestionnaireSubmission
        fields = ["id", "status", "submitted_at", "created_at"]

    @staticmethod
    def resolve_user(obj: QuestionnaireSubmission) -> MinimalRevelUserSchema:
        """Resolve user from submission object."""
        return MinimalRevelUserSchema.from_orm(obj.user)

    @staticmethod
    def resolve_questionnaire_name(obj: QuestionnaireSubmission) -> str:
        """Resolve questionnaire name from submission object."""
        return obj.questionnaire.name

    @staticmethod
    def resolve_evaluation_status(
        obj: QuestionnaireSubmission,
    ) -> QuestionnaireEvaluation.QuestionnaireEvaluationStatus | None:
        """Resolve evaluation status from submission object."""
        if hasattr(obj, "evaluation") and obj.evaluation:
            return obj.evaluation.status  # type: ignore[return-value]
        return None

    @staticmethod
    def resolve_evaluation_score(obj: QuestionnaireSubmission) -> Decimal | None:
        """Resolve evaluation score from submission object."""
        if hasattr(obj, "evaluation") and obj.evaluation:
            return obj.evaluation.score
        return None


class QuestionAnswerDetailSchema(Schema):
    """Schema for question and answer details.

    For multiple choice questions, answer_content is a list of dicts containing:
    - option_id: UUID of the selected option
    - option_text: Text of the selected option
    - is_correct: Boolean indicating if this option is correct

    For free text questions, answer_content is a list with a single dict containing:
    - answer: The free text answer string

    For file upload questions, answer_content is a list of dicts containing:
    - file_id: UUID of the uploaded file
    - original_filename: Original name of the file
    - mime_type: MIME type of the file
    - file_size: Size in bytes
    - file_url: URL to access the file (may be unavailable if user deleted the file)
    """

    question_id: UUID
    question_text: str
    question_type: QuestionType
    reviewer_notes: str | None = None
    answer_content: list[dict[str, t.Any]]


class EvaluationCreateSchema(Schema):
    """Schema for creating/updating an evaluation."""

    status: QuestionnaireEvaluation.QuestionnaireEvaluationStatus
    score: Decimal | None = Field(None, ge=0, le=100)
    comments: str | None = None


class EvaluationResponseSchema(ModelSchema):
    """Schema for evaluation response."""

    id: UUID
    submission_id: UUID
    status: QuestionnaireEvaluation.QuestionnaireEvaluationStatus
    score: Decimal | None
    comments: str | None
    evaluator_id: UUID | None
    created_at: datetime
    updated_at: datetime

    class Meta:
        model = QuestionnaireEvaluation
        fields = ["id", "status", "score", "comments", "created_at", "updated_at"]


class SubmissionDetailSchema(Schema):
    """Schema for detailed view of a submission."""

    id: UUID
    user: MinimalRevelUserSchema
    questionnaire: "QuestionnaireInListSchema"
    status: QuestionnaireSubmission.QuestionnaireSubmissionStatus
    submitted_at: datetime | None
    evaluation: EvaluationResponseSchema | None = None
    answers: list[QuestionAnswerDetailSchema]
    created_at: datetime
    metadata: dict[str, t.Any] | None = None


# Admin schemas


class QuestionnaireBaseSchema(Schema):
    name: str
    description: str | None = None
    status: Questionnaire.QuestionnaireStatus
    min_score: Decimal = Field(ge=0, le=100)
    shuffle_questions: bool = False
    shuffle_sections: bool = False
    evaluation_mode: Questionnaire.QuestionnaireEvaluationMode


class QuestionnaireInListSchema(QuestionnaireBaseSchema):
    id: UUID


class QuestionnaireAdminSchema(QuestionnaireInListSchema):
    id: UUID
    llm_guidelines: str | None = None
    can_retake_after: timedelta | int | None

    _validate_can_retake_after = field_validator("can_retake_after", mode="before")(seconds_to_timedelta)
    _serialize_can_retake_after = field_serializer("can_retake_after")(timedelta_to_seconds)


class FreeTextQuestionCreateSchema(Schema):
    """Schema for creating a FreeTextQuestion."""

    section_id: UUID | None = None
    question: str
    hint: str | None = None
    reviewer_notes: str | None = None
    is_mandatory: bool = False
    order: int = 0
    positive_weight: Decimal = Field(default=Decimal("1.0"), ge=0, le=100)
    negative_weight: Decimal = Field(default=Decimal("0.0"), ge=-100, le=100)
    is_fatal: bool = False
    llm_guidelines: str | None = None
    depends_on_option_id: UUID | None = None


class FreeTextQuestionUpdateSchema(FreeTextQuestionCreateSchema):
    """Schema for updating a FreeTextQuestion."""


class MultipleChoiceOptionCreateSchema(Schema):
    """Schema for creating a MultipleChoiceOption.

    Supports nested conditional questions and sections that will be shown
    only when this option is selected.
    """

    option: str
    is_correct: bool = False
    order: int = 0
    # Forward references - resolved via model_rebuild() at module end
    conditional_mc_questions: list["MultipleChoiceQuestionCreateSchema"] = Field(default_factory=list)
    conditional_ft_questions: list["FreeTextQuestionCreateSchema"] = Field(default_factory=list)
    conditional_fu_questions: list["FileUploadQuestionCreateSchema"] = Field(default_factory=list)
    conditional_sections: list["SectionCreateSchema"] = Field(default_factory=list)


class MultipleChoiceOptionUpdateSchema(Schema):
    """Schema for updating a MultipleChoiceOption."""

    option: str
    is_correct: bool = False
    order: int = 0


class MultipleChoiceOptionResponseSchema(Schema):
    """Schema for MultipleChoiceOption in API responses."""

    id: UUID
    option: str
    is_correct: bool
    order: int


class MultipleChoiceQuestionResponseSchema(Schema):
    """Schema for MultipleChoiceQuestion in API responses.

    Includes options for display after create/update operations.
    """

    id: UUID
    section_id: UUID | None = None
    question: str
    hint: str | None = None
    reviewer_notes: str | None = None
    is_mandatory: bool
    order: int
    positive_weight: Decimal
    negative_weight: Decimal
    is_fatal: bool
    allow_multiple_answers: bool
    shuffle_options: bool
    depends_on_option_id: UUID | None = None
    options: list[MultipleChoiceOptionResponseSchema]

    @staticmethod
    def resolve_options(obj: t.Any) -> list["MultipleChoiceOptionResponseSchema"]:
        """Resolve options from the question object."""
        return [
            MultipleChoiceOptionResponseSchema(
                id=opt.id,
                option=opt.option,
                is_correct=opt.is_correct,
                order=opt.order,
            )
            for opt in obj.options.all()
        ]


class FreeTextQuestionResponseSchema(Schema):
    """Schema for FreeTextQuestion in API responses."""

    id: UUID
    section_id: UUID | None = None
    question: str
    hint: str | None = None
    reviewer_notes: str | None = None
    is_mandatory: bool
    order: int
    positive_weight: Decimal
    negative_weight: Decimal
    is_fatal: bool
    llm_guidelines: str | None = None
    depends_on_option_id: UUID | None = None


class FileUploadQuestionCreateSchema(Schema):
    """Schema for creating a FileUploadQuestion."""

    section_id: UUID | None = None
    question: str
    hint: str | None = None
    reviewer_notes: str | None = None
    is_mandatory: bool = False
    order: int = 0
    # Default positive_weight to 0 since file uploads are informational by default
    positive_weight: Decimal = Field(default=Decimal("0.0"), ge=0, le=100)
    negative_weight: Decimal = Field(default=Decimal("0.0"), ge=-100, le=100)
    is_fatal: bool = False
    allowed_mime_types: list[str] = Field(default_factory=list)
    max_file_size: int = Field(default=5 * 1024 * 1024, gt=0)  # 5MB default
    max_files: int = Field(default=1, ge=1, le=10)
    depends_on_option_id: UUID | None = None

    @field_validator("allowed_mime_types")
    @classmethod
    def validate_mime_types(cls, v: list[str]) -> list[str]:
        """Validate that all MIME types are in the allowed set."""
        if not v:
            return v  # Empty list means all types allowed
        invalid_types = set(v) - ALLOWED_QUESTIONNAIRE_MIME_TYPES
        if invalid_types:
            raise PydanticCustomError(
                "invalid_mime_type",
                "Invalid MIME type(s): {invalid_types}. See ALLOWED_QUESTIONNAIRE_MIME_TYPES.",
                {"invalid_types": ", ".join(sorted(invalid_types))},
            )
        return v


class FileUploadQuestionUpdateSchema(FileUploadQuestionCreateSchema):
    """Schema for updating a FileUploadQuestion."""


class FileUploadQuestionResponseSchema(Schema):
    """Schema for FileUploadQuestion in API responses."""

    id: UUID
    section_id: UUID | None = None
    question: str
    hint: str | None = None
    reviewer_notes: str | None = None
    is_mandatory: bool
    order: int
    positive_weight: Decimal
    negative_weight: Decimal
    is_fatal: bool
    allowed_mime_types: list[str]
    max_file_size: int
    max_files: int
    depends_on_option_id: UUID | None = None


class SectionResponseSchema(Schema):
    """Schema for QuestionnaireSection in API responses."""

    id: UUID
    name: str
    description: str | None = None
    order: int
    depends_on_option_id: UUID | None = None
    multiplechoicequestion_questions: list[MultipleChoiceQuestionResponseSchema] = Field(default_factory=list)
    freetextquestion_questions: list[FreeTextQuestionResponseSchema] = Field(default_factory=list)
    fileuploadquestion_questions: list[FileUploadQuestionResponseSchema] = Field(default_factory=list)


class MultipleChoiceQuestionCreateSchema(Schema):
    """Schema for creating a MultipleChoiceQuestion."""

    section_id: UUID | None = None
    question: str
    hint: str | None = None
    reviewer_notes: str | None = None
    is_mandatory: bool = False
    order: int = 0
    positive_weight: Decimal = Field(default=Decimal("1.0"), ge=0, le=100)
    negative_weight: Decimal = Field(default=Decimal("0.0"), ge=-100, le=100)
    is_fatal: bool = False
    allow_multiple_answers: bool = False
    shuffle_options: bool = True
    options: list[MultipleChoiceOptionCreateSchema]
    depends_on_option_id: UUID | None = None


class MultipleChoiceQuestionUpdateSchema(Schema):
    """Schema for updating a MultipleChoiceQuestion.

    Unlike creation, updates are granular - options must be updated individually
    via the dedicated option endpoints to prevent accidental data loss.
    """

    section_id: UUID | None = None
    question: str
    hint: str | None = None
    reviewer_notes: str | None = None
    is_mandatory: bool = False
    order: int = 0
    positive_weight: Decimal = Field(default=Decimal("1.0"), ge=0, le=100)
    negative_weight: Decimal = Field(default=Decimal("0.0"), ge=-100, le=100)
    is_fatal: bool = False
    allow_multiple_answers: bool = False
    shuffle_options: bool = True
    depends_on_option_id: UUID | None = None


class SectionCreateSchema(Schema):
    """Schema for creating a QuestionnaireSection."""

    name: str
    description: str | None = None
    order: int = 0
    multiplechoicequestion_questions: list[MultipleChoiceQuestionCreateSchema] = Field(default_factory=list)
    freetextquestion_questions: list[FreeTextQuestionCreateSchema] = Field(default_factory=list)
    fileuploadquestion_questions: list[FileUploadQuestionCreateSchema] = Field(default_factory=list)
    depends_on_option_id: UUID | None = None


class SectionUpdateSchema(Schema):
    """Schema for updating a Section.

    Unlike creation, updates are granular - questions must be added/updated individually
    via the dedicated question endpoints to prevent accidental data loss.
    """

    name: str
    description: str | None = None
    order: int = 0
    depends_on_option_id: UUID | None = None


class QuestionnaireCreateSchema(QuestionnaireBaseSchema):
    """Schema for creating a new Questionnaire with its sections and questions."""

    status: Questionnaire.QuestionnaireStatus = Questionnaire.QuestionnaireStatus.DRAFT  # Override to add default
    sections: list[SectionCreateSchema] = Field(default_factory=list)
    multiplechoicequestion_questions: list[MultipleChoiceQuestionCreateSchema] = Field(default_factory=list)
    freetextquestion_questions: list[FreeTextQuestionCreateSchema] = Field(default_factory=list)
    fileuploadquestion_questions: list[FileUploadQuestionCreateSchema] = Field(default_factory=list)
    llm_guidelines: str | None = None
    can_retake_after: timedelta | int | None = None

    _validate_can_retake_after = field_validator("can_retake_after", mode="before")(seconds_to_timedelta)
    _serialize_can_retake_after = field_serializer("can_retake_after")(timedelta_to_seconds)

    @model_validator(mode="after")
    def check_llm_guidelines_for_auto_evaluation(self) -> "QuestionnaireCreateSchema":
        """Validate that LLM guidelines are present.

        If the questionnaire has free-text questions and an automatic or hybrid evaluation mode they are mandatory.
        """
        has_top_level_ftq = self.freetextquestion_questions and len(self.freetextquestion_questions) > 0
        has_section_ftq = any(
            s.freetextquestion_questions and len(s.freetextquestion_questions) > 0 for s in self.sections
        )
        has_free_text = has_top_level_ftq or has_section_ftq

        is_auto_or_hybrid = self.evaluation_mode in [
            Questionnaire.QuestionnaireEvaluationMode.AUTOMATIC,
            Questionnaire.QuestionnaireEvaluationMode.HYBRID,
        ]

        if is_auto_or_hybrid and has_free_text and not self.llm_guidelines:
            raise PydanticCustomError(
                "missing_llm_guidelines",
                "LLM guidelines are required for automatic or hybrid evaluation "
                "of questionnaires with free text questions.",
            )
        return self


class QuestionnaireResponseSchema(QuestionnaireBaseSchema):
    """Schema for Questionnaire in API responses.

    Uses response schemas for nested objects to include id fields needed for editing.
    """

    id: UUID
    sections: list[SectionResponseSchema] = Field(default_factory=list)
    multiplechoicequestion_questions: list[MultipleChoiceQuestionResponseSchema] = Field(default_factory=list)
    freetextquestion_questions: list[FreeTextQuestionResponseSchema] = Field(default_factory=list)
    fileuploadquestion_questions: list[FileUploadQuestionResponseSchema] = Field(default_factory=list)
    llm_guidelines: str | None = None
    can_retake_after: timedelta | int | None = None
    max_attempts: int = 0

    _validate_can_retake_after = field_validator("can_retake_after", mode="before")(seconds_to_timedelta)
    _serialize_can_retake_after = field_serializer("can_retake_after")(timedelta_to_seconds)


# Resolve forward references for nested conditional schemas
MultipleChoiceOptionCreateSchema.model_rebuild()
MultipleChoiceQuestionCreateSchema.model_rebuild()
FileUploadQuestionCreateSchema.model_rebuild()
SectionCreateSchema.model_rebuild()
QuestionnaireCreateSchema.model_rebuild()
SectionResponseSchema.model_rebuild()
QuestionnaireResponseSchema.model_rebuild()
