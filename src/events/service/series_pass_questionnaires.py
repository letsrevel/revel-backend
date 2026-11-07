"""The admission questionnaire verdict a series pass purchase is gated on (#1321).

The set is the series' own admission questionnaires plus those attached to the events a purchase would issue
tickets for (owner ruling: pass tickets must not bypass an event's own questionnaire).
"""

import dataclasses
import datetime
import typing as t
import uuid
from collections import defaultdict

from django.db.models import Exists, OuterRef, Prefetch, Q
from django.utils import timezone
from django.utils.translation import gettext as _

from accounts.models import RevelUser
from events.models import Event, EventQuestionnaireSubmission, OrganizationQuestionnaire, SeriesPass, SeriesPassTierLink
from events.service.event_manager import EventUserEligibility, NextStep, Reasons
from events.service.event_manager.questionnaire_requirements import QuestionnaireRequirementCheck
from events.utils.membership_access import is_eligible_member
from questionnaires.models import Questionnaire, QuestionnaireSubmission

_Lookup = t.Callable[[OrganizationQuestionnaire], list[QuestionnaireSubmission] | None]


@dataclasses.dataclass
class _Group:
    """Questionnaires checked together, with the event their verdict is reported against."""

    event: Event
    questionnaires: list[OrganizationQuestionnaire]
    lookup: _Lookup


def _anchor_event(links: list[SeriesPassTierLink], now: datetime.datetime) -> Event | None:
    """Earliest event still accepting applications, then any open one, then any upcoming."""

    def rank(event: Event) -> tuple[int, datetime.datetime, uuid.UUID]:
        accepting = event.status == Event.EventStatus.OPEN and (event.apply_before is None or event.apply_before >= now)
        open_ = event.status == Event.EventStatus.OPEN
        return (0 if accepting else 1 if open_ else 2, event.start, event.pk)

    return min((link.event for link in links), key=rank, default=None)


def _applicable_questionnaires(
    series_pass: SeriesPass, user: RevelUser, covered_ids: list[uuid.UUID]
) -> list[OrganizationQuestionnaire]:
    """Published admission questionnaires of the series or of a covered event, minus member-exempt ones.

    Each row carries ``is_series`` (linked to the series) and ``covered_events`` (the covered events it is
    attached to), via one query plus one prefetch.
    """
    series = series_pass.event_series
    questionnaires: list[OrganizationQuestionnaire] = list(
        OrganizationQuestionnaire.objects.filter(
            Q(event_series=series) | Q(events__in=covered_ids),
            organization_id=series.organization_id,
            questionnaire_type=OrganizationQuestionnaire.QuestionnaireType.ADMISSION,
            questionnaire__status=Questionnaire.QuestionnaireStatus.PUBLISHED,
        )
        .annotate(
            is_series=Exists(
                OrganizationQuestionnaire.event_series.through.objects.filter(
                    organizationquestionnaire_id=OuterRef("pk"), eventseries_id=series.pk
                )
            )
        )
        .select_related("questionnaire")
        .prefetch_related(
            Prefetch("events", queryset=Event.objects.filter(pk__in=covered_ids).only("id"), to_attr="covered_events")
        )
        .distinct()
    )
    if any(oq.members_exempt for oq in questionnaires) and is_eligible_member(user, series.organization_id):
        questionnaires = [oq for oq in questionnaires if not oq.members_exempt]
    return questionnaires


@dataclasses.dataclass
class _Submissions:
    """The user's READY submissions: global per questionnaire, and per_event ones by questionnaire / by event."""

    global_map: dict[uuid.UUID, list[QuestionnaireSubmission]]
    by_questionnaire: dict[uuid.UUID, list[QuestionnaireSubmission]]
    by_event: dict[tuple[uuid.UUID, uuid.UUID], list[QuestionnaireSubmission]]


def _load_submissions(
    series_pass: SeriesPass, user: RevelUser, questionnaires: list[OrganizationQuestionnaire]
) -> _Submissions:
    """One query per kind (global / per_event)."""
    global_ids = [oq.questionnaire_id for oq in questionnaires if not oq.per_event]
    per_event_ids = [oq.questionnaire_id for oq in questionnaires if oq.per_event]
    found = _Submissions(defaultdict(list), defaultdict(list), defaultdict(list))
    if global_ids:
        for submission in (
            QuestionnaireSubmission.objects.ready()
            .filter(user=user, questionnaire_id__in=global_ids)
            .select_related("evaluation")
        ):
            found.global_map[submission.questionnaire_id].append(submission)
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
            found.by_questionnaire[event_submission.questionnaire_id].append(event_submission.submission)
            found.by_event[(event_submission.questionnaire_id, event_submission.event_id)].append(
                event_submission.submission
            )
    return found


def _event_group(
    event: Event,
    questionnaires: list[OrganizationQuestionnaire],
    submissions: _Submissions,
    seen_global: set[uuid.UUID],
) -> _Group | None:
    """The event-only requirements reported against ``event``, or None if it has none."""
    oqs: list[OrganizationQuestionnaire] = []
    for oq in questionnaires:
        if oq.is_series or event.pk not in {e.pk for e in oq.covered_events}:  # type: ignore[attr-defined]
            continue
        if not oq.per_event:
            # Required once, reported against the earliest covered event carrying it.
            if oq.pk in seen_global:
                continue
            seen_global.add(oq.pk)
        oqs.append(oq)
    if not oqs:
        return None

    def submissions_for(oq: OrganizationQuestionnaire) -> list[QuestionnaireSubmission] | None:
        # An event-only per_event questionnaire needs a submission scoped to this very event.
        if oq.per_event:
            return submissions.by_event.get((oq.questionnaire_id, event.pk)) or None
        return submissions.global_map.get(oq.questionnaire_id) or None

    return _Group(event, oqs, submissions_for)


def _build_groups(
    series_pass: SeriesPass,
    user: RevelUser,
    anchor: Event,
    events: dict[uuid.UUID, Event],
    questionnaires: list[OrganizationQuestionnaire],
) -> list[_Group]:
    """Series group first (anchor event), then one group per covered event holding its event-only requirements."""
    submissions = _load_submissions(series_pass, user, questionnaires)

    def series_lookup(oq: OrganizationQuestionnaire) -> list[QuestionnaireSubmission] | None:
        # A per_event submission made for any event of the series covers the pass.
        source = submissions.by_questionnaire if oq.per_event else submissions.global_map
        return source.get(oq.questionnaire_id) or None

    groups: list[_Group] = []
    series_oqs = [oq for oq in questionnaires if oq.is_series]  # type: ignore[attr-defined]
    if series_oqs:
        groups.append(_Group(anchor, series_oqs, series_lookup))
    seen_global: set[uuid.UUID] = set()
    for event in sorted(events.values(), key=lambda e: (e.start, e.pk)):
        group = _event_group(event, questionnaires, submissions, seen_global)
        if group is not None:
            groups.append(group)
    return groups


def check_series_questionnaires(
    series_pass: SeriesPass, user: RevelUser, *, owner_or_staff: bool | None = None
) -> EventUserEligibility | None:
    """The admission questionnaire verdict a pass purchase is gated on (#1321), or None if clear.

    Required: the series' admission questionnaires (reported against the anchor event, the earliest
    upcoming covered event still taking applications) plus each upcoming covered event's own admission
    questionnaires. A questionnaire linked to both is required once, as a series one. An event-only
    questionnaire is required once, reported against the earliest covered event carrying it, unless it is
    ``per_event``: then every covered event carrying it needs a submission scoped to that event.

    Groups (series first, then covered events by start) are checked separately. If any is missing
    questionnaires, ONE verdict lists every missing id and points at the first such group's event;
    otherwise the first non-None verdict wins. Owners and staff bypass; with no upcoming covered event
    there is nothing to gate. Event invitations never waive these questionnaires.

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
    links = list(series_pass.tier_links.filter(event__start__gte=now).select_related("event"))
    anchor = _anchor_event(links, now)
    if anchor is None:
        return None
    events = {link.event_id: link.event for link in links}
    questionnaires = _applicable_questionnaires(series_pass, user, list(events))
    if not questionnaires:
        return None
    verdicts: list[tuple[Event, EventUserEligibility]] = []
    for group in _build_groups(series_pass, user, anchor, events, questionnaires):
        verdict = QuestionnaireRequirementCheck(group.event.id, group.questionnaires, group.lookup).check()
        if verdict is not None:
            verdicts.append((group.event, verdict))
    if not verdicts:
        return None
    missing = [(e, v) for e, v in verdicts if v.next_step == NextStep.COMPLETE_QUESTIONNAIRE]
    if missing:
        target, result = missing[0]
        all_missing = list(dict.fromkeys(q for _, v in missing for q in v.questionnaires_missing or []))
        result = result.model_copy(update={"questionnaires_missing": all_missing})
    else:
        target, result = verdicts[0]
    can_submit = target.status == Event.EventStatus.OPEN and now <= target.effective_apply_deadline
    if result.next_step == NextStep.COMPLETE_QUESTIONNAIRE and not can_submit:
        # The questionnaire is outstanding but the event it points at can no longer take a submission.
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
