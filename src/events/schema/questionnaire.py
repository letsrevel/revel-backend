"""Organization questionnaire schemas."""

from datetime import timedelta
from decimal import Decimal
from uuid import UUID

from ninja import Schema
from pydantic import Field, field_serializer

from events.models import OrganizationQuestionnaire
from questionnaires import schema as questionnaires_schema
from questionnaires.models import Questionnaire

from .event import MinimalEventSchema
from .event_series import MinimalEventSeriesSchema

# ===== Summary schemas =====


class McOptionStatSchema(Schema):
    option_id: UUID
    option_text: str
    is_correct: bool
    count: int


class McQuestionStatSchema(Schema):
    question_id: UUID
    question_text: str
    options: list[McOptionStatSchema]


class StatusBreakdownSchema(Schema):
    approved: int = 0
    rejected: int = 0
    pending_review: int = 0
    not_evaluated: int = 0


class ScoreStatsSchema(Schema):
    avg: Decimal | None = None
    min: Decimal | None = None
    max: Decimal | None = None


class QuestionnaireSummarySchema(Schema):
    total_submissions: int
    unique_users: int
    by_status: StatusBreakdownSchema
    by_status_per_user: StatusBreakdownSchema
    score_stats: ScoreStatsSchema
    mc_question_stats: list[McQuestionStatSchema]


class BaseOrganizationQuestionnaireSchema(Schema):
    id: UUID
    events: list[MinimalEventSchema] = Field(default_factory=list)
    event_series: list[MinimalEventSeriesSchema] = Field(default_factory=list)
    max_submission_age: timedelta | int | None = None
    questionnaire_type: OrganizationQuestionnaire.QuestionnaireType
    members_exempt: bool
    per_event: bool

    @field_serializer("max_submission_age")
    def serialize_max_submission_age(self, value: timedelta | int | None) -> int | None:
        """Convert timedelta to seconds for serialization."""
        if value is None:
            return None
        if isinstance(value, timedelta):
            return int(value.total_seconds())
        return value


class OrganizationQuestionnaireInListSchema(BaseOrganizationQuestionnaireSchema):
    questionnaire: questionnaires_schema.QuestionnaireInListSchema
    pending_evaluations_count: int = 0


class OrganizationQuestionnaireSchema(BaseOrganizationQuestionnaireSchema):
    questionnaire: questionnaires_schema.QuestionnaireResponseSchema


class OrganizationQuestionnaireFieldsMixin(Schema):
    """Mixin for OrganizationQuestionnaire-specific fields."""

    max_submission_age: timedelta | None = None
    questionnaire_type: OrganizationQuestionnaire.QuestionnaireType = (
        OrganizationQuestionnaire.QuestionnaireType.ADMISSION
    )
    members_exempt: bool = False
    per_event: bool = False


class OrganizationQuestionnaireCreateSchema(
    questionnaires_schema.QuestionnaireCreateSchema, OrganizationQuestionnaireFieldsMixin
):
    """Schema for creating OrganizationQuestionnaire with its underlying Questionnaire.

    Combines Questionnaire creation fields (name, sections, questions, etc.) with
    OrganizationQuestionnaire wrapper fields (max_submission_age, questionnaire_type).
    """

    pass


class OrganizationQuestionnaireUpdateSchema(Schema):
    """Schema for updating OrganizationQuestionnaire and its underlying Questionnaire.

    Includes fields from both OrganizationQuestionnaire (wrapper) and Questionnaire (the actual questionnaire).
    All fields are optional to allow partial updates.
    """

    # Questionnaire fields (from QuestionnaireBaseSchema + additional)
    name: str | None = None
    min_score: Decimal | None = Field(None, ge=0, le=100)
    shuffle_questions: bool | None = None
    shuffle_sections: bool | None = None
    evaluation_mode: Questionnaire.QuestionnaireEvaluationMode | None = None
    llm_guidelines: str | None = None
    can_retake_after: timedelta | None = None
    max_attempts: int = Field(0, ge=0)

    # OrganizationQuestionnaire wrapper fields
    max_submission_age: timedelta | None = None
    questionnaire_type: OrganizationQuestionnaire.QuestionnaireType | None = None
    members_exempt: bool | None = None
    per_event: bool | None = None


class EventAssignmentSchema(Schema):
    event_ids: list[UUID]


class EventSeriesAssignmentSchema(Schema):
    event_series_ids: list[UUID]
