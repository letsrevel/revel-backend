"""Tests for the GDPR service."""

import json
import zipfile
from io import BytesIO

import pytest

from accounts.models import RevelUser, UserDataExport
from accounts.service import gdpr
from questionnaires.models import Questionnaire, QuestionnaireEvaluation, QuestionnaireSubmission
from questionnaires.schema import (
    FreeTextQuestionCreateSchema,
    MultipleChoiceQuestionCreateSchema,
)
from questionnaires.service import QuestionnaireService


@pytest.mark.django_db
def test_generate_user_data_export(user_with_questionnaire_submission: RevelUser) -> None:
    """Test that the user data export is generated correctly."""
    export = gdpr.generate_user_data_export(user_with_questionnaire_submission)

    assert export.user == user_with_questionnaire_submission
    assert export.status == UserDataExport.Status.READY
    assert export.file is not None

    with zipfile.ZipFile(BytesIO(export.file.read()), "r") as zip_file:
        assert "revel_user_data.json" in zip_file.namelist()
        with zip_file.open("revel_user_data.json") as json_file:
            data = json.load(json_file)
            assert "profile" in data
            assert "questionnaire_submissions" in data
            assert len(data["questionnaire_submissions"]) == 1


@pytest.mark.django_db
def test_serialize_questionnaire_data(user: RevelUser, questionnaire: Questionnaire) -> None:
    """Test that the questionnaire data is serialized correctly."""
    QuestionnaireSubmission.objects.create(user=user, questionnaire=questionnaire)
    service = QuestionnaireService(questionnaire.id)
    service.create_mc_question(
        payload=MultipleChoiceQuestionCreateSchema.model_validate({"question": "MCQ", "options": [{"option": "A"}]})
    )
    service.create_ft_question(payload=FreeTextQuestionCreateSchema(question="FTQ"))

    data = gdpr._serialize_questionnaire_data(user.id)

    assert "questionnaire_submissions" in data
    assert len(data["questionnaire_submissions"]) == 1
    assert data["questionnaire_submissions"][0]["questionnaire_name"] == questionnaire.name


@pytest.mark.django_db
def test_generate_user_data_export_no_questionnaire(user: RevelUser) -> None:
    """Test that the user data export is generated correctly when the user has no questionnaire submissions."""
    export = gdpr.generate_user_data_export(user)

    assert export.user == user
    assert export.status == UserDataExport.Status.READY
    assert export.file is not None

    with zipfile.ZipFile(BytesIO(export.file.read()), "r") as zip_file:
        assert "revel_user_data.json" in zip_file.namelist()
        with zip_file.open("revel_user_data.json") as json_file:
            data = json.load(json_file)
            assert "profile" in data
            assert data["questionnaire_submissions"] == []


@pytest.mark.django_db
def test_serialize_questionnaire_data_with_evaluation(user: RevelUser, questionnaire: Questionnaire) -> None:
    """Test that the questionnaire data is serialized correctly when there is an evaluation."""
    submission = QuestionnaireSubmission.objects.create(
        user=user, questionnaire=questionnaire, status=QuestionnaireSubmission.Status.READY
    )
    QuestionnaireEvaluation.objects.create(submission=submission, status="approved", score=100)

    data = gdpr._serialize_questionnaire_data(user.id)

    assert "questionnaire_submissions" in data
    assert len(data["questionnaire_submissions"]) == 1
    assert data["questionnaire_submissions"][0]["evaluation"] is not None
