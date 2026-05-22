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

    The owning organization is taken from the URL path, not the payload:
    ``POST /api/polls/organizations/{organization_id}``.
    """

    min_score: Decimal = Field(default=Decimal(0), ge=0, le=100)
    evaluation_mode: Questionnaire.QuestionnaireEvaluationMode = Questionnaire.QuestionnaireEvaluationMode.MANUAL

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

    The controller calls ``model_dump(exclude_unset=True)``: fields not
    present in the request body are ignored (unset semantics). An explicit
    ``None`` in the request body CLEARS the field on the poll instead of
    leaving it untouched. Validators below short-circuit on the ``"event_id"
    in self.model_fields_set`` check to honour that distinction.

    ``name`` and ``description`` apply to the wrapped ``Questionnaire``,
    not to the ``Poll`` itself. They're editable at any lifecycle stage
    (DRAFT/OPEN/CLOSED) — the signal lockdown in :mod:`polls.signals`
    only blocks question/option/section mutations, not questionnaire
    metadata.
    """

    name: str | None = None
    description: str | None = None
    vote_visibility: ResourceVisibility | None = None
    result_visibility: ResourceVisibility | None = None
    result_timing: Poll.PollResultTiming | None = None
    vote_membership_tier_ids: list[UUID] | None = None
    result_membership_tier_ids: list[UUID] | None = None
    allow_vote_changes: bool | None = None
    closes_at: AwareDatetime | None = None
    event_id: UUID | None = None

    @model_validator(mode="after")
    def _validate_cross_field(self) -> t.Self:
        """Reject combinations that would violate Poll CheckConstraints.

        This catches the most obvious case where the SAME payload sets
        ``event_id=None`` together with a restricted visibility. Stale-state
        cases (e.g., poll already has PRIVATE visibility and the patch only
        clears ``event_id``) cannot be detected here without the current
        instance — the controller catches the resulting DB ``ValidationError``
        and translates it to 422.

        Also rejects an explicit ``name=null`` since the wrapped
        ``Questionnaire.name`` is non-nullable.
        """
        # event_id explicitly cleared in this payload?
        event_id_cleared = "event_id" in self.model_fields_set and self.event_id is None
        if event_id_cleared:
            restricted = {ResourceVisibility.PRIVATE, ResourceVisibility.ATTENDEES_ONLY}
            if self.vote_visibility in restricted or self.result_visibility in restricted:
                raise ValueError(
                    "Cannot clear event_id while setting vote/result visibility to PRIVATE or ATTENDEES_ONLY."
                )
        if "name" in self.model_fields_set and self.name is None:
            raise ValueError("Cannot clear the poll name; it is required.")
        return self


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
    """List-item view for :class:`polls.models.Poll`.

    Per-user flags (``user_has_voted`` / ``user_can_vote`` / ``user_can_see_results``)
    are computed from the per-row annotations attached by
    :meth:`polls.models.PollQuerySet.with_user_annotations`. The list controller
    composes that method into the queryset; detail/lifecycle controllers should
    use :class:`PollDetailSchema` instead.
    """

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

    @staticmethod
    def resolve_questionnaire_name(obj: Poll) -> str:
        """Read the wrapped questionnaire's display name."""
        return obj.questionnaire.name

    @staticmethod
    def resolve_user_has_voted(obj: Poll, context: t.Any) -> bool:
        """Whether the requesting user has a READY submission for this poll."""
        from polls.service.eligibility import user_has_voted_from_annotations

        user = context["request"].user
        return user_has_voted_from_annotations(user, obj)

    @staticmethod
    def resolve_user_can_vote(obj: Poll, context: t.Any) -> bool:
        """Whether the requesting user is currently eligible to cast a vote.

        Returns False for anonymous users and for polls that are not OPEN.
        """
        from polls.service.eligibility import can_vote_from_annotations

        user = context["request"].user
        if user.is_anonymous:
            return False
        if obj.status != Poll.PollStatus.OPEN:
            return False
        tier_ids = [tier.id for tier in obj.vote_membership_tiers.all()]
        return can_vote_from_annotations(user, obj, tier_ids)

    @staticmethod
    def resolve_user_can_see_results(obj: Poll, context: t.Any) -> bool:
        """Whether the requesting user can view aggregate results right now."""
        from polls.service.eligibility import can_see_results_from_annotations

        user = context["request"].user
        tier_ids = [tier.id for tier in obj.result_membership_tiers.all()]
        return can_see_results_from_annotations(user, obj, tier_ids)


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
