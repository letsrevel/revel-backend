"""Ninja Schema classes for the polls API."""

import typing as t
from decimal import Decimal
from uuid import UUID

from ninja import Schema
from pydantic import AwareDatetime, Field, model_validator

from events.models.mixins import ResourceVisibility
from events.schema.questionnaire import McQuestionStatSchema
from polls.models import Poll
from questionnaires import schema as questionnaires_schema
from questionnaires.models import Questionnaire

# ----- Create / Update -----


class PollCreateSchema(questionnaires_schema.QuestionnaireCreateSchema):
    """Create a poll along with its underlying questionnaire.

    Inherits questionnaire-creation fields (name, sections, questions, options).
    ``evaluation_mode`` is forced to ``MANUAL`` server-side and ``min_score`` is
    irrelevant for polls — both default here so clients only need to supply
    poll-specific fields.
    """

    min_score: Decimal = Field(default=Decimal(0), ge=0, le=100)
    evaluation_mode: Questionnaire.QuestionnaireEvaluationMode = Questionnaire.QuestionnaireEvaluationMode.MANUAL

    organization_id: UUID
    event_id: UUID | None = None
    vote_visibility: ResourceVisibility
    result_visibility: ResourceVisibility = ResourceVisibility.STAFF_ONLY
    result_timing: Poll.PollResultTiming = Poll.PollResultTiming.NEVER
    vote_membership_tier_ids: list[UUID] = Field(default_factory=list)
    result_membership_tier_ids: list[UUID] = Field(default_factory=list)
    staff_anonymous: bool = True
    public_anonymous: bool = True
    allow_vote_changes: bool = False
    closes_at: AwareDatetime | None = None


class PollUpdateSchema(Schema):
    """PATCH payload. Anonymity flags are forbidden after creation.

    ``None`` values are treated as "do not update" — fields use exclude_unset semantics.
    """

    vote_visibility: ResourceVisibility | None = None
    result_visibility: ResourceVisibility | None = None
    result_timing: Poll.PollResultTiming | None = None
    vote_membership_tier_ids: list[UUID] | None = None
    result_membership_tier_ids: list[UUID] | None = None
    allow_vote_changes: bool | None = None
    closes_at: AwareDatetime | None = None
    event_id: UUID | None = None


class PollReopenSchema(Schema):
    """Reopen a closed poll, optionally setting or clearing the ``closes_at`` deadline."""

    closes_at: AwareDatetime | None = None
    clear_closes_at: bool = False

    @model_validator(mode="after")
    def _validate(self) -> t.Self:
        if self.closes_at is not None and self.clear_closes_at:
            raise ValueError("Set either closes_at or clear_closes_at, not both.")
        return self


# ----- Vote payload -----


class McAnswerInput(Schema):
    question_id: UUID
    option_ids: list[UUID]


class FreeTextAnswerInput(Schema):
    question_id: UUID
    answer: str


class FileUploadAnswerInput(Schema):
    question_id: UUID
    file_ids: list[UUID]


class PollVoteSchema(Schema):
    mc_answers: list[McAnswerInput] = Field(default_factory=list)
    free_text_answers: list[FreeTextAnswerInput] = Field(default_factory=list)
    file_upload_answers: list[FileUploadAnswerInput] = Field(default_factory=list)


# ----- Read schemas -----


class PollDetailSchema(Schema):
    id: UUID
    organization_id: UUID
    event_id: UUID | None
    questionnaire_id: UUID
    status: Poll.PollStatus
    opened_at: AwareDatetime | None
    closes_at: AwareDatetime | None
    closed_at: AwareDatetime | None
    allow_vote_changes: bool
    vote_visibility: ResourceVisibility
    result_visibility: ResourceVisibility
    result_timing: Poll.PollResultTiming
    staff_anonymous: bool
    public_anonymous: bool
    vote_membership_tier_ids: list[UUID]
    result_membership_tier_ids: list[UUID]
    user_has_voted: bool
    user_can_vote: bool
    user_can_see_results: bool
    questionnaire: questionnaires_schema.QuestionnaireResponseSchema | None = None
    results: "PollResultsSchema | None" = None


class PollListItemSchema(Schema):
    id: UUID
    organization_id: UUID
    event_id: UUID | None
    questionnaire_name: str
    status: Poll.PollStatus
    opened_at: AwareDatetime | None
    closes_at: AwareDatetime | None
    closed_at: AwareDatetime | None
    vote_visibility: ResourceVisibility
    result_visibility: ResourceVisibility
    user_has_voted: bool
    user_can_vote: bool
    user_can_see_results: bool


# ----- Results -----


class PollFreeTextResponseSchema(Schema):
    question_id: UUID
    answer: str
    answered_at: AwareDatetime
    user_id: UUID | None = None  # populated only when the viewer is allowed to see voter identity


class PollResultsSchema(Schema):
    total_voters: int
    mc_question_stats: list[McQuestionStatSchema] = Field(default_factory=list)
    free_text_responses: list[PollFreeTextResponseSchema] = Field(default_factory=list)


PollDetailSchema.model_rebuild()
