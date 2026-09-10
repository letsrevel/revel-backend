"""Unit tests for the shared questionnaire retake/staleness policy helpers."""

import datetime

import pytest
from django.utils import timezone

from questionnaires.models import Questionnaire
from questionnaires.utils.retake_policy import (
    RetakeDecision,
    RetakeVerdict,
    approval_is_stale,
    evaluate_retake_policy,
)

HOUR = datetime.timedelta(hours=1)


def _questionnaire(*, max_attempts: int = 0, can_retake_after: datetime.timedelta | None = None) -> Questionnaire:
    """Build an unsaved questionnaire carrying only the retake-policy fields."""
    return Questionnaire(name="q", max_attempts=max_attempts, can_retake_after=can_retake_after)


# --- approval_is_stale ---


def test_approval_is_stale_without_max_age() -> None:
    """No configured max age means an approval never goes stale."""
    assert approval_is_stale(None, timezone.now() - datetime.timedelta(days=365)) is False


def test_approval_is_stale_with_zero_max_age() -> None:
    """A zero max age is treated as "no expiry" (falsy), not "expires instantly"."""
    assert approval_is_stale(datetime.timedelta(0), timezone.now() - HOUR) is False


def test_approval_is_stale_when_elapsed() -> None:
    """An approval older than the max age is stale."""
    now = timezone.now()
    assert approval_is_stale(HOUR, now - datetime.timedelta(hours=2), now=now) is True


def test_approval_is_stale_when_fresh() -> None:
    """An approval younger than the max age is not stale."""
    now = timezone.now()
    assert approval_is_stale(HOUR, now - datetime.timedelta(minutes=30), now=now) is False


def test_approval_is_stale_at_the_exact_expiry_instant() -> None:
    """At exactly ``evaluated_at + max_submission_age`` the approval is still valid."""
    now = timezone.now()
    assert approval_is_stale(HOUR, now - HOUR, now=now) is False


# --- evaluate_retake_policy: attempts cap (checked first, both flag values) ---


@pytest.mark.parametrize("terminal", [True, False])
def test_attempts_cap_outranks_the_cooldown(terminal: bool) -> None:
    """The attempts cap is evaluated before the cooldown, for both NULL semantics."""
    now = timezone.now()
    decision = evaluate_retake_policy(
        _questionnaire(max_attempts=2, can_retake_after=HOUR),
        last_submitted_at=now - datetime.timedelta(days=1),
        attempts=2,
        null_cooldown_is_terminal=terminal,
        now=now,
    )
    assert decision == RetakeDecision(RetakeVerdict.ATTEMPTS_EXHAUSTED)


@pytest.mark.parametrize("terminal", [True, False])
def test_zero_max_attempts_means_unlimited(terminal: bool) -> None:
    """``max_attempts=0`` never exhausts, however many submissions exist."""
    now = timezone.now()
    decision = evaluate_retake_policy(
        _questionnaire(max_attempts=0, can_retake_after=None),
        last_submitted_at=now,
        attempts=99,
        null_cooldown_is_terminal=terminal,
        now=now,
    )
    assert decision.verdict is (RetakeVerdict.NO_RETAKE if terminal else RetakeVerdict.RETAKE_NOW)


def test_attempts_below_the_cap_falls_through_to_the_cooldown() -> None:
    """Under the cap the cooldown decides."""
    now = timezone.now()
    decision = evaluate_retake_policy(
        _questionnaire(max_attempts=3, can_retake_after=HOUR),
        last_submitted_at=now - datetime.timedelta(hours=2),
        attempts=1,
        null_cooldown_is_terminal=False,
        now=now,
    )
    assert decision == RetakeDecision(RetakeVerdict.RETAKE_NOW)


# --- evaluate_retake_policy: NULL cooldown semantics ---


def test_null_cooldown_is_immediate_retake_for_admission() -> None:
    """Admission questionnaires read NULL ``can_retake_after`` as "no cooldown"."""
    decision = evaluate_retake_policy(
        _questionnaire(can_retake_after=None),
        last_submitted_at=timezone.now(),
        attempts=1,
        null_cooldown_is_terminal=False,
    )
    assert decision == RetakeDecision(RetakeVerdict.RETAKE_NOW)


def test_null_cooldown_is_terminal_for_membership() -> None:
    """Membership questionnaires read NULL ``can_retake_after`` as "no retake"."""
    decision = evaluate_retake_policy(
        _questionnaire(can_retake_after=None),
        last_submitted_at=timezone.now(),
        attempts=1,
        null_cooldown_is_terminal=True,
    )
    assert decision == RetakeDecision(RetakeVerdict.NO_RETAKE)


# --- evaluate_retake_policy: cooldown window ---


@pytest.mark.parametrize("terminal", [True, False])
def test_cooldown_still_running(terminal: bool) -> None:
    """Inside the cooldown window the decision carries the retry instant."""
    now = timezone.now()
    submitted_at = now - datetime.timedelta(minutes=30)
    decision = evaluate_retake_policy(
        _questionnaire(can_retake_after=HOUR),
        last_submitted_at=submitted_at,
        attempts=1,
        null_cooldown_is_terminal=terminal,
        now=now,
    )
    assert decision == RetakeDecision(RetakeVerdict.COOLDOWN, retry_on=submitted_at + HOUR)


@pytest.mark.parametrize("terminal", [True, False])
def test_cooldown_elapsed(terminal: bool) -> None:
    """Past the cooldown window the user may retake."""
    now = timezone.now()
    decision = evaluate_retake_policy(
        _questionnaire(can_retake_after=HOUR),
        last_submitted_at=now - datetime.timedelta(hours=2),
        attempts=1,
        null_cooldown_is_terminal=terminal,
        now=now,
    )
    assert decision == RetakeDecision(RetakeVerdict.RETAKE_NOW)


def test_cooldown_boundary_allows_the_retake() -> None:
    """At exactly ``retry_on`` the cooldown has elapsed — the user may retake.

    The four call sites disagreed on this instant before the extraction: three
    blocked only while ``retry_on > now`` while the admission eligibility gate
    blocked while ``retry_on >= now``. The majority (and friendlier) reading wins.
    """
    now = timezone.now()
    decision = evaluate_retake_policy(
        _questionnaire(can_retake_after=HOUR),
        last_submitted_at=now - HOUR,
        attempts=1,
        null_cooldown_is_terminal=False,
        now=now,
    )
    assert decision == RetakeDecision(RetakeVerdict.RETAKE_NOW)


def test_zero_cooldown_allows_immediate_retake() -> None:
    """A zero-length cooldown behaves like no cooldown at all."""
    now = timezone.now()
    decision = evaluate_retake_policy(
        _questionnaire(can_retake_after=datetime.timedelta(0)),
        last_submitted_at=now,
        attempts=1,
        null_cooldown_is_terminal=True,
        now=now,
    )
    assert decision == RetakeDecision(RetakeVerdict.RETAKE_NOW)
