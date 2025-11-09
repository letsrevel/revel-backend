import typing as t
from datetime import datetime

import pytest
from django.core.exceptions import ValidationError
from django.utils import timezone

from accounts.models import RevelUser
from events import exceptions
from events.models import (
    AdditionalResource,
    Event,
    EventSeries,
    Organization,
    OrganizationStaff,
)
from events.models.organization import _validate_permissions


@pytest.mark.django_db
def test_organization_creation(organization_owner_user: RevelUser) -> None:
    org = Organization.objects.create(name="Org1", slug="org1", owner=organization_owner_user)
    assert org.name == "Org1"
    assert org.owner == organization_owner_user


@pytest.mark.django_db
def test_unique_organizaion_staff_member(organization: Organization, organization_staff_user: RevelUser) -> None:
    OrganizationStaff.objects.create(organization=organization, user=organization_staff_user)
    with pytest.raises(ValidationError):
        OrganizationStaff(organization=organization, user=organization_staff_user).full_clean()


@pytest.mark.django_db
def test_permissions_schema_validates(organization: Organization, organization_staff_user: RevelUser) -> None:
    member = OrganizationStaff.objects.create(organization=organization, user=organization_staff_user)
    assert member.permissions["default"]["create_event"] is False


@pytest.mark.django_db
def test_event_series_unique_constraint(organization: Organization) -> None:
    EventSeries.objects.create(organization=organization, name="Series1", slug="series1")
    with pytest.raises(ValidationError):
        EventSeries(organization=organization, name="Series1", slug="series2").full_clean()


@pytest.mark.django_db
def test_event_creation(event: Event) -> None:
    assert event.organization.name == "Org"
    assert event.event_type == Event.EventType.PUBLIC


@pytest.mark.django_db
def test_event_with_organization_queryset(event: Event) -> None:
    qs = Event.objects.with_organization()
    p = qs.get(id=event.id)
    assert hasattr(p.organization, "staff_members")


@pytest.mark.django_db
def test_event_resource_valid_file(event: Event, tmp_path: t.Any) -> None:
    f = tmp_path / "file.txt"
    f.write_text("content")
    resource = AdditionalResource(
        organization=event.organization,
        resource_type=AdditionalResource.ResourceTypes.FILE,
        file=f.open("rb"),
    )
    resource.full_clean()


@pytest.mark.django_db
def test_event_resource_invalid_multiple_fields(event: Event) -> None:
    resource = AdditionalResource(
        organization=event.organization,
        resource_type=AdditionalResource.ResourceTypes.LINK,
        link="https://example.com",
        text="Should not be here",
    )
    with pytest.raises(exceptions.InvalidResourceStateError):
        resource.clean()


@pytest.mark.django_db
def test_event_resource_missing_required_field(event: Event) -> None:
    resource = AdditionalResource(
        organization=event.organization,
        resource_type=AdditionalResource.ResourceTypes.TEXT,
        # Missing text field
    )
    with pytest.raises(exceptions.InvalidResourceStateError):
        resource.clean()


def test_validate_permissions() -> None:
    with pytest.raises(ValidationError):
        _validate_permissions({"foo": "bar"})


@pytest.mark.django_db
def test_organization_staff_has_permission(
    organization: Organization, organization_staff_user: RevelUser, event: Event
) -> None:
    # Create a staff member with specific permissions
    staff = OrganizationStaff.objects.create(
        organization=organization,
        user=organization_staff_user,
        permissions={
            "default": {
                "edit_organization": True,
                "create_event": False,
            },
            "event_overrides": {
                str(event.id): {
                    "edit_event": True,
                    "delete_event": False,
                }
            },
        },
    )

    # Test default permissions
    assert staff.has_permission("edit_organization") is True
    assert staff.has_permission("create_event") is False
    assert staff.has_permission("non_existent_permission") is False

    # Test event-specific permissions
    assert staff.has_permission("edit_event", str(event.id)) is True
    assert staff.has_permission("delete_event", str(event.id)) is False
    assert staff.has_permission("non_existent_permission", str(event.id)) is False

    # Test with non-existent event_id
    assert staff.has_permission("edit_event", "non-existent-id") is False


@pytest.mark.django_db
def test_event_save_preserves_explicit_end_date(organization: Organization) -> None:
    """Test that Event.save() preserves explicitly set end date."""

    start_time = timezone.make_aware(datetime(2024, 3, 15, 14, 30))  # 2:30 PM
    end_time = timezone.make_aware(datetime(2024, 3, 15, 18, 0))  # 6:00 PM same day

    event = Event.objects.create(organization=organization, name="Test Event", start=start_time, end=end_time)

    # Check that explicitly set end date is preserved
    assert event.end == end_time


@pytest.mark.django_db
def test_event_ics_generation(event: Event) -> None:
    """Test that Event.ics() generates valid iCalendar content."""
    ics_content = event.ics()

    # Should return bytes
    assert isinstance(ics_content, bytes)

    # Convert to string for content checks
    ics_str = ics_content.decode("utf-8")

    # Should contain iCalendar headers
    assert "BEGIN:VCALENDAR" in ics_str
    assert "END:VCALENDAR" in ics_str
    assert "BEGIN:VEVENT" in ics_str
    assert "END:VEVENT" in ics_str

    # Should contain event details
    assert event.name in ics_str
    assert f"UID:{event.id}@letsrevel.io" in ics_str


@pytest.mark.django_db
def test_event_ics_with_address(organization: Organization) -> None:
    """Test that Event.ics() includes address when available."""
    event = Event.objects.create(
        organization=organization, name="Test Event", start=timezone.now(), address="123 Test Street"
    )

    ics_content = event.ics()
    ics_str = ics_content.decode("utf-8")

    # Should include the address
    assert "123 Test Street" in ics_str


@pytest.mark.django_db
def test_event_ics_with_city_no_address(organization: Organization) -> None:
    """Test that Event.ics() uses city name when no address is provided."""
    from geo.models import City

    # Create a city for the test
    city = City.objects.first()
    assert city is not None

    event = Event.objects.create(organization=organization, name="Test Event", start=timezone.now(), city=city)

    ics_content = event.ics()
    ics_str = ics_content.decode("utf-8")

    # Should include the city name
    assert city.name in ics_str


@pytest.mark.django_db
def test_event_ics_no_location(organization: Organization) -> None:
    """Test that Event.ics() handles events with no location information."""
    event = Event.objects.create(organization=organization, name="Test Event", start=timezone.now())

    ics_content = event.ics()
    ics_str = ics_content.decode("utf-8")

    # Should include fallback text
    assert "See event details" in ics_str
