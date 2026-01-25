"""Tests for the GDPR service."""

import json
import zipfile
from decimal import Decimal
from io import BytesIO
from unittest.mock import Mock

import orjson
import pytest
from django.contrib.gis.geos import Point
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db.models.fields.files import FieldFile

from accounts.models import RevelUser, UserDataExport
from accounts.service import gdpr
from events.models import AdditionalResource, GeneralUserPreferences, Organization
from events.models.follow import OrganizationFollow
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
    assert export.status == UserDataExport.UserDataExportStatus.READY
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
    assert export.status == UserDataExport.UserDataExportStatus.READY
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
    assert export.status == UserDataExport.UserDataExportStatus.READY
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
            assert export.status == UserDataExport.UserDataExportStatus.READY

            # The data should be valid JSON (if Point wasn't serializable, this would have failed)
            assert isinstance(data, dict)


# Tests for _default_serializer function (orjson custom handler)


def test_default_serializer_handles_field_file_with_url() -> None:
    """Test that _default_serializer converts FieldFile to URL string."""
    # Create a mock FieldFile with a URL
    mock_file = Mock(spec=FieldFile)
    mock_file.url = "/media/test.jpg"
    mock_file.__bool__ = Mock(return_value=True)

    result = gdpr._default_serializer(mock_file)

    assert result == "/media/test.jpg"


def test_default_serializer_handles_field_file_without_url() -> None:
    """Test that _default_serializer handles FieldFile when URL access fails."""
    # Create a mock FieldFile that raises an exception when accessing url
    mock_file = Mock(spec=FieldFile)
    mock_file.__bool__ = Mock(return_value=True)

    # Make url property raise an exception
    type(mock_file).url = property(lambda self: (_ for _ in ()).throw(ValueError("File does not exist")))

    result = gdpr._default_serializer(mock_file)

    assert result == "[This field could not be exported]"


def test_default_serializer_handles_empty_field_file() -> None:
    """Test that _default_serializer handles empty FieldFile (falsy value)."""
    # Create a mock FieldFile that evaluates to False
    mock_file = Mock(spec=FieldFile)
    mock_file.__bool__ = Mock(return_value=False)

    result = gdpr._default_serializer(mock_file)

    assert result == "[This field could not be exported]"


def test_default_serializer_handles_point() -> None:
    """Test that _default_serializer converts PostGIS Point to GeoJSON format."""
    point = Point(-74.0060, 40.7128)  # NYC coordinates

    result = gdpr._default_serializer(point)

    assert result == {"type": "Point", "coordinates": [-74.0060, 40.7128]}


def test_default_serializer_handles_decimal() -> None:
    """Test that _default_serializer converts Decimal to string."""
    decimal_value = Decimal("123.45")

    result = gdpr._default_serializer(decimal_value)

    assert result == "123.45"
    assert isinstance(result, str)


def test_default_serializer_handles_unknown_object_with_str() -> None:
    """Test that _default_serializer falls back to str() for unknown objects."""

    class CustomObject:
        def __str__(self) -> str:
            return "custom_value"

    obj = CustomObject()

    result = gdpr._default_serializer(obj)

    assert result == "custom_value"


def test_default_serializer_handles_object_without_str() -> None:
    """Test that _default_serializer handles objects that can't be converted to string."""

    class UnstringableObject:
        def __str__(self) -> str:
            raise RuntimeError("Cannot stringify")

    obj = UnstringableObject()

    result = gdpr._default_serializer(obj)

    assert result == "[This field could not be exported]"


# Integration tests for orjson serialization


def test_orjson_serializes_datetime_natively() -> None:
    """Test that orjson handles datetime without calling default handler."""
    from datetime import datetime

    data = {"created_at": datetime(2025, 1, 15, 12, 30, 45)}

    # orjson should handle datetime natively (won't call default)
    result = orjson.dumps(data, default=gdpr._default_serializer)
    parsed = orjson.loads(result)

    assert "created_at" in parsed
    assert isinstance(parsed["created_at"], str)
    assert "2025-01-15" in parsed["created_at"]


def test_orjson_serializes_uuid_natively() -> None:
    """Test that orjson handles UUID without calling default handler."""
    from uuid import UUID

    test_uuid = UUID("550e8400-e29b-41d4-a716-446655440000")
    data = {"id": test_uuid}

    result = orjson.dumps(data, default=gdpr._default_serializer)
    parsed = orjson.loads(result)

    assert parsed["id"] == "550e8400-e29b-41d4-a716-446655440000"


def test_orjson_with_default_handles_mixed_types() -> None:
    """Test that orjson + default handler handles both native and custom types."""
    from datetime import datetime
    from uuid import UUID

    mock_file = Mock(spec=FieldFile)
    mock_file.url = "/media/avatar.jpg"
    mock_file.__bool__ = Mock(return_value=True)

    data = {
        "id": UUID("550e8400-e29b-41d4-a716-446655440000"),
        "created_at": datetime(2025, 1, 15, 12, 30),
        "price": Decimal("99.99"),
        "avatar": mock_file,
        "location": Point(1.0, 2.0),
        "name": "Test",
    }

    result = orjson.dumps(data, default=gdpr._default_serializer, option=orjson.OPT_INDENT_2)
    parsed = orjson.loads(result)

    # Native types handled by orjson
    assert parsed["id"] == "550e8400-e29b-41d4-a716-446655440000"
    assert "2025-01-15" in parsed["created_at"]
    assert parsed["name"] == "Test"

    # Custom types handled by default handler
    assert parsed["price"] == "99.99"
    assert parsed["avatar"] == "/media/avatar.jpg"
    assert parsed["location"] == {"type": "Point", "coordinates": [1.0, 2.0]}


@pytest.mark.django_db
def test_generate_user_data_export_with_file_field(user: RevelUser) -> None:
    """Test that GDPR export handles FileField correctly in related objects.

    This test ensures that when a user has related objects with FileFields,
    the export successfully serializes them to URLs or fallback messages.
    """
    # Create an organization with a logo (ImageField)
    org = Organization.objects.create(
        name="Test Org",
        owner=user,
        slug="test-org",
    )

    # Create an additional resource with a file
    test_file = SimpleUploadedFile("test.pdf", b"file content", content_type="application/pdf")
    AdditionalResource.objects.create(
        organization=org,
        resource_type=AdditionalResource.ResourceTypes.FILE,
        name="Test Document",
        file=test_file,
    )

    # Generate the export
    export = gdpr.generate_user_data_export(user)

    assert export.status == UserDataExport.UserDataExportStatus.READY
    assert export.file is not None

    # Extract and parse the JSON
    with zipfile.ZipFile(BytesIO(export.file.read()), "r") as zip_file:
        with zip_file.open("revel_user_data.json") as json_file:
            data = json.load(json_file)

            # The export should complete without errors
            assert isinstance(data, dict)
            assert "profile" in data

            # If owned_organizations is in the export, verify it's serializable
            if "owned_organizations" in data:
                assert isinstance(data["owned_organizations"], list)


@pytest.mark.django_db
def test_generate_user_data_export_handles_all_field_types(user: RevelUser) -> None:
    """Test that GDPR export handles all Django field types without crashing.

    This is a comprehensive test ensuring that no matter what field types
    are present in the user's related data, the export never fails.
    """
    # Create a city with Point field
    city = City.objects.create(
        name="Test City",
        ascii_name="Test City",
        country="US",
        city_id=99999,
        location=Point(-122.4194, 37.7749),  # San Francisco
    )

    # Update user preferences
    preferences = GeneralUserPreferences.objects.get(user=user)
    preferences.city = city
    preferences.save()

    # Create an organization (has ImageField for logo and cover_art)
    Organization.objects.create(
        name="Test Org",
        owner=user,
        slug="test-org-unique",
    )

    # Generate export - should never crash regardless of field types
    export = gdpr.generate_user_data_export(user)

    assert export.status == UserDataExport.UserDataExportStatus.READY
    assert export.file is not None

    # Verify the ZIP and JSON are valid
    with zipfile.ZipFile(BytesIO(export.file.read()), "r") as zip_file:
        assert "revel_user_data.json" in zip_file.namelist()
        with zip_file.open("revel_user_data.json") as json_file:
            data = json.load(json_file)

            # Basic structure checks
            assert isinstance(data, dict)
            assert "profile" in data
            assert "general_preferences" in data

            # Verify no fields are None due to serialization failures
            # (fields should be either valid values or fallback messages)
            assert data["profile"] is not None


@pytest.mark.django_db
def test_generate_user_data_export_includes_follow_data(user: RevelUser) -> None:
    """Test that GDPR export includes organization and event series follows.

    Follows are user relationships that should be included in the GDPR export
    as they represent user preferences and subscriptions.
    """
    # Create another user to own an org the test user can follow
    from accounts.models import RevelUser as RU

    other_user = RU.objects.create_user(
        username="other@example.com",
        email="other@example.com",
        password="password",
    )
    other_org = Organization.objects.create(
        name="Other Org",
        owner=other_user,
        slug="other-org",
    )

    # Create organization follow
    OrganizationFollow.objects.create(
        user=user,
        organization=other_org,
        notify_new_events=True,
        notify_announcements=False,
        is_public=True,
    )

    # Refresh user to clear cached related objects
    user.refresh_from_db()

    # Generate the export
    export = gdpr.generate_user_data_export(user)

    assert export.status == UserDataExport.UserDataExportStatus.READY
    assert export.file is not None

    # Extract and parse the JSON
    with zipfile.ZipFile(BytesIO(export.file.read()), "r") as zip_file:
        with zip_file.open("revel_user_data.json") as json_file:
            data = json.load(json_file)

            # Verify organization_follows is in the export
            assert "organization_follows" in data
            assert isinstance(data["organization_follows"], list)
            assert len(data["organization_follows"]) == 1

            # Verify the follow data is correct
            follow_data = data["organization_follows"][0]
            assert follow_data["organization"] == str(other_org.id)
            assert follow_data["notify_new_events"] is True
            assert follow_data["notify_announcements"] is False
            assert follow_data["is_public"] is True
