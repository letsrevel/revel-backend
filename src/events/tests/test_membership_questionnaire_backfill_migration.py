"""Tests for the data migration pinning one-shot membership questionnaires to max_attempts=1."""

import importlib
import typing as t

import pytest
from django.apps import apps

from events.models import Organization, OrganizationQuestionnaire
from questionnaires.models import Questionnaire

pytestmark = pytest.mark.django_db

backfill = importlib.import_module("events.migrations.0138_backfill_membership_questionnaire_max_attempts")


def _make(
    organization: Organization,
    qtype: str,
    *,
    can_retake_after: t.Any = None,
    max_attempts: int = 0,
) -> Questionnaire:
    q = Questionnaire.objects.create(
        name="Q",
        status=Questionnaire.QuestionnaireStatus.PUBLISHED,
        can_retake_after=can_retake_after,
        max_attempts=max_attempts,
    )
    OrganizationQuestionnaire.objects.create(organization=organization, questionnaire=q, questionnaire_type=qtype)
    return q


def _run_and_reload(q: Questionnaire) -> int:
    backfill.forward(apps, None)
    q.refresh_from_db()
    return q.max_attempts


def test_membership_null_cooldown_unlimited_becomes_one_shot(organization: Organization) -> None:
    q = _make(organization, OrganizationQuestionnaire.QuestionnaireType.MEMBERSHIP)
    assert _run_and_reload(q) == 1


def test_membership_with_cooldown_unchanged(organization: Organization) -> None:
    import datetime

    q = _make(
        organization,
        OrganizationQuestionnaire.QuestionnaireType.MEMBERSHIP,
        can_retake_after=datetime.timedelta(days=1),
    )
    assert _run_and_reload(q) == 0


def test_membership_with_max_attempts_unchanged(organization: Organization) -> None:
    q = _make(organization, OrganizationQuestionnaire.QuestionnaireType.MEMBERSHIP, max_attempts=3)
    assert _run_and_reload(q) == 3


def test_admission_null_cooldown_unchanged(organization: Organization) -> None:
    q = _make(organization, OrganizationQuestionnaire.QuestionnaireType.ADMISSION)
    assert _run_and_reload(q) == 0
