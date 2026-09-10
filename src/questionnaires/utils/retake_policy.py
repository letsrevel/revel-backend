"""The single source of truth for questionnaire retake and staleness policy.

Both the admission path (``events.service.event_manager.gates.QuestionnaireGate`` /
``events.service.event_questionnaire_service``) and the membership path
(``events.service.membership_manager.gates.MembershipQuestionnaireGate`` /
``events.service.membership_questionnaire_service``) decide whether a user may
resubmit a questionnaire. They used to answer that question with four
hand-written copies of the same rules, which had drifted apart. Both a gate and
its submit endpoint must agree exactly — a gate promising SUBMIT sends the user
to a guaranteed 400 otherwise — so the rules live here.

The two paths genuinely disagree on one thing: what a NULL ``can_retake_after``
means. Admission reads it as "no cooldown, retake immediately"; membership reads
it as "no retake at all, terminal failure". That is expressed by the explicit
``null_cooldown_is_terminal`` flag rather than by two divergent copies.

This module is pure: no queries, no ``events`` imports.
"""

from __future__ import annotations

import dataclasses
import datetime
import enum
import typing as t

from django.utils import timezone

if t.TYPE_CHECKING:
    from questionnaires.models import Questionnaire


class RetakeVerdict(enum.StrEnum):
    """What the retake policy allows after a rejected evaluation."""

    RETAKE_NOW = "retake_now"
    """The user may submit again right away (treated as "questionnaire missing")."""

    COOLDOWN = "cooldown"
    """The user must wait until ``RetakeDecision.retry_on``."""

    ATTEMPTS_EXHAUSTED = "attempts_exhausted"
    """``max_attempts`` reached — terminal, whatever the cooldown says."""

    NO_RETAKE = "no_retake"
    """No cooldown configured and NULL means "never" for this path — terminal."""


@dataclasses.dataclass(frozen=True, slots=True)
class RetakeDecision:
    """The verdict plus, for :attr:`RetakeVerdict.COOLDOWN` only, when to retry."""

    verdict: RetakeVerdict
    retry_on: datetime.datetime | None = None


def approval_is_stale(
    max_submission_age: datetime.timedelta | None,
    evaluated_at: datetime.datetime,
    *,
    now: datetime.datetime | None = None,
) -> bool:
    """Whether an APPROVED evaluation has aged out of ``max_submission_age``.

    Args:
        max_submission_age: How long an approval stays valid. Falsy (NULL or a
            zero duration) means approvals never expire.
        evaluated_at: When the approval was last written (``evaluation.updated_at``).
        now: Reference instant, for tests. Defaults to ``timezone.now()``.

    Returns:
        True when the approval has expired and a fresh submission is required.
    """
    if not max_submission_age:
        return False
    return (evaluated_at + max_submission_age) < (now or timezone.now())


def evaluate_retake_policy(
    questionnaire: Questionnaire,
    *,
    last_submitted_at: datetime.datetime,
    attempts: int,
    null_cooldown_is_terminal: bool,
    now: datetime.datetime | None = None,
) -> RetakeDecision:
    """Decide whether a user whose evaluation was REJECTED may submit again.

    The attempts cap is checked first: a user at the cap can never satisfy the
    questionnaire again, so telling them to wait out a cooldown that expires
    into a guaranteed rejection would be a lie.

    A cooldown blocks only while ``retry_on`` is still in the future; at exactly
    ``retry_on`` the cooldown has elapsed and the retake is allowed.

    Args:
        questionnaire: Supplies ``max_attempts`` and ``can_retake_after``.
        last_submitted_at: ``submitted_at`` of the user's most recent READY
            submission for this questionnaire.
        attempts: How many READY submissions the user has made.
        null_cooldown_is_terminal: How to read a NULL ``can_retake_after`` —
            False for admission questionnaires ("no cooldown"), True for
            membership questionnaires ("no retake").
        now: Reference instant, for tests. Defaults to ``timezone.now()``.

    Returns:
        The :class:`RetakeDecision`; ``retry_on`` is set for COOLDOWN only.
    """
    if 0 < questionnaire.max_attempts <= attempts:
        return RetakeDecision(RetakeVerdict.ATTEMPTS_EXHAUSTED)

    if questionnaire.can_retake_after is None:
        return RetakeDecision(RetakeVerdict.NO_RETAKE if null_cooldown_is_terminal else RetakeVerdict.RETAKE_NOW)

    retry_on = last_submitted_at + questionnaire.can_retake_after
    if retry_on > (now or timezone.now()):
        return RetakeDecision(RetakeVerdict.COOLDOWN, retry_on=retry_on)
    return RetakeDecision(RetakeVerdict.RETAKE_NOW)
