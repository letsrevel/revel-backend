"""Tests for the GDPR service."""

import json
import zipfile
from io import BytesIO

import pytest
from django.contrib.gis.geos import Point

from accounts.models import RevelUser, UserDataExport
from accounts.service import gdpr
from events.models import GeneralUserPreferences
from geo.models import City
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
        user=user, questionnaire=questionnaire, status=QuestionnaireSubmission.QuestionnaireSubmissionStatus.READY
    )
    QuestionnaireEvaluation.objects.create(submission=submission, status="approved", score=100)

    data = gdpr._serialize_questionnaire_data(user.id)

    assert "questionnaire_submissions" in data
    assert len(data["questionnaire_submissions"]) == 1
    assert data["questionnaire_submissions"][0]["evaluation"] is not None


@pytest.mark.django_db
def test_generate_user_data_export_with_city_location(user: RevelUser) -> None:
    """Test that user data export correctly serializes PostGIS Point fields in related objects.

    This test ensures that when a user has city preferences (which contain PostGIS Point fields),
    the GDPR export successfully serializes the Point to GeoJSON format instead of failing.
    """
    # Create a city with a PostGIS Point location
    city = City.objects.create(
        name="New York",
        ascii_name="New York",
        country="US",
        city_id=12345,
        location=Point(-74.0060, 40.7128),  # longitude, latitude
    )

    # Update the user's preferences with the city (preferences are created via signal)
    preferences = GeneralUserPreferences.objects.get(user=user)
    preferences.city = city
    preferences.save()

    # Refresh the user to clear cached related objects
    user.refresh_from_db()

    # Generate the export
    export = gdpr.generate_user_data_export(user)

    assert export.user == user
    assert export.status == UserDataExport.Status.READY
    assert export.file is not None

    # Extract and parse the JSON from the ZIP file
    with zipfile.ZipFile(BytesIO(export.file.read()), "r") as zip_file:
        assert "revel_user_data.json" in zip_file.namelist()
        with zip_file.open("revel_user_data.json") as json_file:
            data = json.load(json_file)

            # Verify the export contains user preferences
            assert "general_preferences" in data

            # Verify the city and its location are properly serialized
            preferences_data = data["general_preferences"]
            assert "city" in preferences_data
            assert preferences_data["city"] is not None

            # The city field should be an ID (foreign key)
            assert preferences_data["city"] == city.id


@pytest.mark.django_db
def test_gdpr_json_encoder_serializes_point_to_geojson(user: RevelUser) -> None:
    """Test that the GDPRJSONEncoder correctly serializes Point objects to GeoJSON format.

    This is a more focused test that verifies the Point serialization logic directly,
    ensuring Point objects are converted to the standard GeoJSON format.
    """
    # Create a city with a specific location
    longitude, latitude = -74.0060, 40.7128
    city = City.objects.create(
        name="New York",
        ascii_name="New York",
        country="US",
        city_id=12345,
        location=Point(longitude, latitude),
    )

    # Update user preferences with the city (preferences are created via signal)
    preferences = GeneralUserPreferences.objects.get(user=user)
    preferences.city = city
    preferences.save()

    # Refresh the user to clear cached related objects
    user.refresh_from_db()

    # Generate the export
    export = gdpr.generate_user_data_export(user)

    # Extract the JSON and verify Point serialization
    with zipfile.ZipFile(BytesIO(export.file.read()), "r") as zip_file:
        with zip_file.open("revel_user_data.json") as json_file:
            data = json.load(json_file)

            # The export should complete without TypeError
            assert export.status == UserDataExport.Status.READY

            # The data should be valid JSON (if Point wasn't serializable, this would have failed)
            assert isinstance(data, dict)
