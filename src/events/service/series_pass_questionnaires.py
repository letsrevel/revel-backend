"""The series admission questionnaire verdict a series pass purchase is gated on (#1321)."""

import datetime
import typing as t
import uuid
from collections import defaultdict

from django.db.models import Case, IntegerField, Q, Value, When
from django.utils import timezone
from django.utils.translation import gettext as _

from accounts.models import RevelUser
from events.models import Event, EventQuestionnaireSubmission, OrganizationQuestionnaire, SeriesPass
from events.service.event_manager import EventUserEligibility, NextStep, Reasons
from events.service.event_manager.questionnaire_requirements import QuestionnaireRequirementCheck
from events.utils.membership_access import is_eligible_member
from questionnaires.models import Questionnaire, QuestionnaireSubmission


def _anchor_event(series_pass: SeriesPass, now: datetime.datetime) -> Event | None:
    """Earliest event still accepting applications, then any open one, then any upcoming (one query)."""
    accepting = Q(event__status=Event.EventStatus.OPEN) & (
        Q(event__apply_before__isnull=True) | Q(event__apply_before__gte=now)
    )
    link = (
        series_pass.tier_links.filter(event__start__gte=now)
        .select_related("event")
        .annotate(
            anchor_rank=Case(
                When(accepting, then=Value(0)),
                When(event__status=Event.EventStatus.OPEN, then=Value(1)),
                default=Value(2),
                output_field=IntegerField(),
            )
        )
        .order_by("anchor_rank", "event__start")
        .first()
    )
    return link.event if link else None


def _applicable_questionnaires(series_pass: SeriesPass, user: RevelUser) -> list[OrganizationQuestionnaire]:
    """Published admission questionnaires linked to the series, minus those the user is exempt from as a member."""
    series = series_pass.event_series
    questionnaires = list(
        OrganizationQuestionnaire.objects.filter(
            organization_id=series.organization_id,
            event_series=series,
            questionnaire_type=OrganizationQuestionnaire.QuestionnaireType.ADMISSION,
            questionnaire__status=Questionnaire.QuestionnaireStatus.PUBLISHED,
        ).select_related("questionnaire")
    )
    if any(oq.members_exempt for oq in questionnaires) and is_eligible_member(user, series.organization_id):
        questionnaires = [oq for oq in questionnaires if not oq.members_exempt]
    return questionnaires


def _submission_lookup(
    series_pass: SeriesPass, user: RevelUser, questionnaires: list[OrganizationQuestionnaire]
) -> t.Callable[[OrganizationQuestionnaire], list[QuestionnaireSubmission] | None]:
    """Build a newest-first submission lookup, using one query per kind (global / per_event)."""
    global_ids = [oq.questionnaire_id for oq in questionnaires if not oq.per_event]
    per_event_ids = [oq.questionnaire_id for oq in questionnaires if oq.per_event]

    global_map: dict[uuid.UUID, list[QuestionnaireSubmission]] = defaultdict(list)
    if global_ids:
        for submission in (
            QuestionnaireSubmission.objects.ready()
            .filter(user=user, questionnaire_id__in=global_ids)
            .select_related("evaluation")
        ):
            global_map[submission.questionnaire_id].append(submission)

    # A per_event submission made for any event of the series covers the pass.
    per_event_map: dict[uuid.UUID, list[QuestionnaireSubmission]] = defaultdict(list)
    if per_event_ids:
        for event_submission in (
            EventQuestionnaireSubmission.objects.filter(
                user=user,
                event__event_series_id=series_pass.event_series_id,
                questionnaire_id__in=per_event_ids,
                submission__status=QuestionnaireSubmission.QuestionnaireSubmissionStatus.READY,
            )
            .select_related("submission__evaluation")
            .order_by("-submission__submitted_at")
        ):
            per_event_map[event_submission.questionnaire_id].append(event_submission.submission)

    def submissions_for(oq: OrganizationQuestionnaire) -> list[QuestionnaireSubmission] | None:
        source = per_event_map if oq.per_event else global_map
        return source.get(oq.questionnaire_id) or None

    return submissions_for


def check_series_questionnaires(
    series_pass: SeriesPass, user: RevelUser, *, owner_or_staff: bool | None = None
) -> EventUserEligibility | None:
    """The series admission questionnaire verdict a pass purchase is gated on (#1321), or None if clear.

    The check runs against the earliest upcoming covered event, scoped to questionnaires
    linked to the series (event-only questionnaires are not a pass-purchase concern).
    Owners and staff bypass; with no upcoming covered event there is nothing to gate.
    Event invitations never waive the series questionnaire.

    Args:
        series_pass: The pass being purchased.
        user: The purchasing user.
        owner_or_staff: Pass a value the caller already computed to skip that query.

    Returns:
        The blocking eligibility verdict, or None if the user may proceed.
    """
    if owner_or_staff is None:
        owner_or_staff = series_pass.event_series.organization.is_owner_or_staff(user)
    if owner_or_staff:
        return None
    now = timezone.now()
    anchor = _anchor_event(series_pass, now)
    if anchor is None:
        return None
    questionnaires = _applicable_questionnaires(series_pass, user)
    if not questionnaires:
        return None
    result = QuestionnaireRequirementCheck(
        anchor.id, questionnaires, _submission_lookup(series_pass, user, questionnaires)
    ).check()
    if result is None:
        return None
    can_submit = anchor.status == Event.EventStatus.OPEN and now <= anchor.effective_apply_deadline
    if result.next_step == NextStep.COMPLETE_QUESTIONNAIRE and not can_submit:
        # The questionnaire is outstanding but no covered event can take a submission any more.
        reason = str(_("The series questionnaire can no longer be submitted, so this pass cannot be purchased."))
        result = result.model_copy(
            update={"reason": reason, "reason_code": Reasons.APPLICATION_DEADLINE_PASSED.code, "next_step": None}
        )
    return result


def missing_series_questionnaires(series_pass: SeriesPass, user: RevelUser) -> tuple[list[uuid.UUID], uuid.UUID | None]:
    """Questionnaires the user can still answer to unlock the pass, and the event to answer them through."""
    result = check_series_questionnaires(series_pass, user)
    if result is None or result.next_step != NextStep.COMPLETE_QUESTIONNAIRE:
        return [], None
    return list(result.questionnaires_missing or []), result.event_id
