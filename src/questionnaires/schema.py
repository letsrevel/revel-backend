import typing as t
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

from ninja import ModelSchema, Schema
from pydantic import Field, field_serializer, model_validator
from pydantic_core import PydanticCustomError

from questionnaires.models import Questionnaire, QuestionnaireEvaluation, QuestionnaireSubmission


class BaseUUIDSchema(Schema):
    id: UUID


class BaseQuestionSchema(BaseUUIDSchema):
    question: str
    is_mandatory: bool
    order: int


class MultipleChoiceOptionSchema(BaseUUIDSchema):
    option: str
    order: int


class MultipleChoiceQuestionSchema(BaseQuestionSchema):
    allow_multiple_answers: bool
    options: list[MultipleChoiceOptionSchema]


class FreeTextQuestionSchema(BaseQuestionSchema):
    pass


class QuestionContainerSchema(BaseUUIDSchema):
    name: str
    multiple_choice_questions: list[MultipleChoiceQuestionSchema] = Field(default_factory=list)
    free_text_questions: list[FreeTextQuestionSchema] = Field(default_factory=list)


class SectionSchema(QuestionContainerSchema):
    order: int


class QuestionnaireSchema(QuestionContainerSchema):
    sections: list[SectionSchema] = Field(default_factory=list)


# --- Questionnaire Submission ---


class MultipleChoiceSubmissionSchema(Schema):
    question_id: UUID
    options_id: list[UUID]


class FreeTextSubmissionSchema(Schema):
    question_id: UUID
    answer: str = Field(..., min_length=1, max_length=500)


class QuestionnaireSubmissionSchema(Schema):
    questionnaire_id: UUID
    multiple_choice_answers: list[MultipleChoiceSubmissionSchema] = Field(default_factory=list)
    free_text_answers: list[FreeTextSubmissionSchema] = Field(default_factory=list)
    status: QuestionnaireSubmission.Status

    @model_validator(mode="after")
    def ensure_unique_question_ids(self) -> "QuestionnaireSubmissionSchema":
        """A validator to ensure unique question ids are not repeated."""
        all_question_ids = [mc.question_id for mc in self.multiple_choice_answers] + [
            ft.question_id for ft in self.free_text_answers
        ]

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
    status: QuestionnaireSubmission.Status
    submitted_at: datetime

    class Meta:
        model = QuestionnaireSubmission
        fields = ["status", "submitted_at"]


class QuestionnaireEvaluationForUserSchema(ModelSchema):
    submission: QuestionnaireSubmissionResponseSchema
    score: Decimal
    status: QuestionnaireEvaluation.Status

    class Meta:
        model = QuestionnaireEvaluation
        fields = ["submission", "score", "status"]


QuestionnaireSubmissionOrEvaluationSchema = QuestionnaireSubmissionResponseSchema | QuestionnaireEvaluationForUserSchema


# Submission management schemas for organization staff


class SubmissionListItemSchema(ModelSchema):
    """Schema for listing submissions for organization staff."""

    id: UUID
    user_email: str
    user_name: str
    questionnaire_name: str
    evaluation_status: QuestionnaireEvaluation.Status | None = None
    evaluation_score: Decimal | None = None

    class Meta:
        model = QuestionnaireSubmission
        fields = ["id", "status", "submitted_at", "created_at"]

    @staticmethod
    def resolve_user_email(obj: QuestionnaireSubmission) -> str:
        """Resolve user email from submission object."""
        return obj.user.email

    @staticmethod
    def resolve_user_name(obj: QuestionnaireSubmission) -> str:
        """Resolve user name from submission object."""
        return obj.user.preferred_name or f"{obj.user.first_name} {obj.user.last_name}".strip()

    @staticmethod
    def resolve_questionnaire_name(obj: QuestionnaireSubmission) -> str:
        """Resolve questionnaire name from submission object."""
        return obj.questionnaire.name

    @staticmethod
    def resolve_evaluation_status(obj: QuestionnaireSubmission) -> QuestionnaireEvaluation.Status | None:
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
    """Schema for question and answer details."""

    question_id: UUID
    question_text: str
    question_type: str  # "multiple_choice" or "free_text"
    # Store answer content directly as JSON for flexibility
    answer_content: dict[str, t.Any]


class EvaluationCreateSchema(Schema):
    """Schema for creating/updating an evaluation."""

    status: QuestionnaireEvaluation.Status
    score: Decimal | None = Field(None, ge=0, le=100)
    comments: str | None = None


class EvaluationResponseSchema(ModelSchema):
    """Schema for evaluation response."""

    id: UUID
    submission_id: UUID
    status: QuestionnaireEvaluation.Status
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
    user_email: str
    user_name: str
    questionnaire: "QuestionnaireInListSchema"
    status: QuestionnaireSubmission.Status
    submitted_at: datetime | None
    evaluation: EvaluationResponseSchema | None = None
    answers: list[QuestionAnswerDetailSchema]
    created_at: datetime


# Admin schemas


class QuestionnaireBaseSchema(Schema):
    name: str
    min_score: Decimal = Field(ge=0, le=100)
    shuffle_questions: bool = False
    shuffle_sections: bool = False
    evaluation_mode: Questionnaire.EvaluationMode


class QuestionnaireInListSchema(QuestionnaireBaseSchema):
    id: UUID


class QuestionnaireAdminSchema(QuestionnaireInListSchema):
    id: UUID
    llm_guidelines: str | None = None
    can_retake_after: timedelta | int | None

    @field_serializer("can_retake_after")
    def serialize_can_retake_after(self, value: timedelta | int | None) -> int | None:
        """Convert timedelta to seconds for serialization."""
        if value is None:
            return None
        if isinstance(value, timedelta):
            return int(value.total_seconds())
        return value


class FreeTextQuestionCreateSchema(Schema):
    """Schema for creating a FreeTextQuestion."""

    section_id: UUID | None = None
    question: str
    is_mandatory: bool = False
    order: int = 0
    positive_weight: Decimal = Field(default=Decimal("1.0"), ge=0, le=100)
    negative_weight: Decimal = Field(default=Decimal("0.0"), ge=-100, le=100)
    is_fatal: bool = False
    llm_guidelines: str | None = None


class FreeTextQuestionUpdateSchema(FreeTextQuestionCreateSchema):
    """Schema for updating a FreeTextQuestion."""


class MultipleChoiceOptionCreateSchema(Schema):
    """Schema for creating a MultipleChoiceOption."""

    option: str
    is_correct: bool = False
    order: int = 0


class MultipleChoiceOptionUpdateSchema(MultipleChoiceOptionCreateSchema):
    """Schema for updating a MultipleChoiceOption."""


class MultipleChoiceQuestionCreateSchema(Schema):
    """Schema for creating a MultipleChoiceQuestion."""

    section_id: UUID | None = None
    question: str
    is_mandatory: bool = False
    order: int = 0
    positive_weight: Decimal = Field(default=Decimal("1.0"), ge=0, le=100)
    negative_weight: Decimal = Field(default=Decimal("0.0"), ge=-100, le=100)
    is_fatal: bool = False
    allow_multiple_answers: bool = False
    shuffle_options: bool = True
    options: list[MultipleChoiceOptionCreateSchema]


class MultipleChoiceQuestionUpdateSchema(MultipleChoiceQuestionCreateSchema):
    """Schema for updating a MultipleChoiceQuestion."""

    options: list[MultipleChoiceOptionCreateSchema]

    @model_validator(mode="before")
    @classmethod
    def check_options(cls, data: Any) -> Any:
        """Ensure that options is not None."""
        if isinstance(data, dict) and data.get("options") is None:
            data["options"] = []
        return data


class SectionCreateSchema(Schema):
    """Schema for creating a QuestionnaireSection."""

    name: str
    order: int = 0
    multiplechoicequestion_questions: list[MultipleChoiceQuestionCreateSchema] = Field(default_factory=list)
    freetextquestion_questions: list[FreeTextQuestionCreateSchema] = Field(default_factory=list)


class SectionUpdateSchema(SectionCreateSchema):
    """Schema for updating a Section."""


class QuestionnaireCreateSchema(QuestionnaireBaseSchema):
    """Schema for creating a new Questionnaire with its sections and questions."""

    sections: list[SectionCreateSchema] = Field(default_factory=list)
    multiplechoicequestion_questions: list[MultipleChoiceQuestionCreateSchema] = Field(default_factory=list)
    freetextquestion_questions: list[FreeTextQuestionCreateSchema] = Field(default_factory=list)
    llm_guidelines: str | None = None
    can_retake_after: timedelta | int | None = None

    @field_serializer("can_retake_after")
    def serialize_can_retake_after(self, value: timedelta | int | None) -> int | None:
        """Convert timedelta to seconds for serialization."""
        if value is None:
            return None
        if isinstance(value, timedelta):
            return int(value.total_seconds())
        return value

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
            Questionnaire.EvaluationMode.AUTOMATIC,
            Questionnaire.EvaluationMode.HYBRID,
        ]

        if is_auto_or_hybrid and has_free_text and not self.llm_guidelines:
            raise PydanticCustomError(
                "missing_llm_guidelines",
                "LLM guidelines are required for automatic or hybrid evaluation "
                "of questionnaires with free text questions.",
            )
        return self
