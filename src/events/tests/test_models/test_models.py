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
    MembershipTier,
    Organization,
    OrganizationMember,
    OrganizationStaff,
    OrganizationToken,
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


@pytest.mark.django_db
def test_organization_member_tier_validation_success(
    organization: Organization, organization_owner_user: RevelUser
) -> None:
    """Test that OrganizationMember.clean() passes when tier belongs to same organization."""
    tier = MembershipTier.objects.create(organization=organization, name="Gold")
    member = OrganizationMember(organization=organization, user=organization_owner_user, tier=tier)

    # Should not raise any exception
    member.full_clean()


@pytest.mark.django_db
def test_organization_member_tier_validation_fails_wrong_organization(
    organization: Organization, organization_owner_user: RevelUser
) -> None:
    """Test that OrganizationMember.clean() fails when tier belongs to different organization."""
    other_org = Organization.objects.create(name="Other Org", slug="other-org", owner=organization_owner_user)
    tier = MembershipTier.objects.create(organization=other_org, name="Gold")
    member = OrganizationMember(organization=organization, user=organization_owner_user, tier=tier)

    with pytest.raises(ValidationError) as exc_info:
        member.full_clean()

    assert "tier" in exc_info.value.message_dict
    assert "must belong to the same organization" in str(exc_info.value.message_dict["tier"])


@pytest.mark.django_db
def test_organization_member_tier_can_be_null(organization: Organization, organization_owner_user: RevelUser) -> None:
    """Test that OrganizationMember can have no tier assigned (tier=None)."""
    member = OrganizationMember(organization=organization, user=organization_owner_user, tier=None)

    # Should not raise any exception
    member.full_clean()
    member.save()
    assert member.tier is None


@pytest.mark.django_db
def test_organization_token_requires_tier_when_granting_membership(
    organization: Organization, organization_owner_user: RevelUser
) -> None:
    """Test that OrganizationToken requires membership_tier when grants_membership is True."""
    token = OrganizationToken(
        organization=organization,
        issuer=organization_owner_user,
        grants_membership=True,
        membership_tier=None,  # Missing tier
    )

    with pytest.raises(ValidationError) as exc_info:
        token.full_clean()

    assert "membership_tier" in exc_info.value.message_dict
    assert "required when granting membership" in str(exc_info.value.message_dict["membership_tier"])


@pytest.mark.django_db
def test_organization_token_tier_must_match_organization(
    organization: Organization, organization_owner_user: RevelUser
) -> None:
    """Test that membership_tier must belong to the same organization as the token."""
    other_org = Organization.objects.create(name="Other Org", slug="other-org", owner=organization_owner_user)
    other_tier = MembershipTier.objects.create(organization=other_org, name="Other Tier")

    token = OrganizationToken(
        organization=organization,
        issuer=organization_owner_user,
        grants_membership=True,
        membership_tier=other_tier,  # Tier from different organization
    )

    with pytest.raises(ValidationError) as exc_info:
        token.full_clean()

    assert "membership_tier" in exc_info.value.message_dict
    assert "must belong to the same organization" in str(exc_info.value.message_dict["membership_tier"])


@pytest.mark.django_db
def test_organization_token_valid_with_tier(organization: Organization, organization_owner_user: RevelUser) -> None:
    """Test that OrganizationToken validates successfully with correct tier."""
    tier = MembershipTier.objects.create(organization=organization, name="Gold")

    token = OrganizationToken(
        organization=organization,
        issuer=organization_owner_user,
        grants_membership=True,
        membership_tier=tier,
    )

    # Should not raise any exception
    token.full_clean()
    token.save()
    assert token.membership_tier == tier


@pytest.mark.django_db
def test_organization_token_tier_optional_when_not_granting_membership(
    organization: Organization, organization_owner_user: RevelUser
) -> None:
    """Test that membership_tier is optional when grants_membership is False."""
    token = OrganizationToken(
        organization=organization,
        issuer=organization_owner_user,
        grants_membership=False,
        grants_staff_status=True,
        membership_tier=None,
    )

    # Should not raise any exception
    token.full_clean()
    token.save()
    assert token.membership_tier is None


# --- Tests for SlugFromNameMixin slug collision handling ---


@pytest.mark.django_db
def test_event_slug_auto_generated(organization: Organization) -> None:
    """Test that slug is auto-generated from name."""
    event = Event.objects.create(
        organization=organization,
        name="My Test Event",
        start=timezone.now(),
    )
    assert event.slug == "my-test-event"


@pytest.mark.django_db
def test_event_slug_collision_appends_suffix(organization: Organization) -> None:
    """Test that slug collision appends a random suffix."""
    event1 = Event.objects.create(
        organization=organization,
        name="Weekly Reading Circle",
        start=timezone.now(),
    )
    assert event1.slug == "weekly-reading-circle"

    # Create second event with same name - should get a suffix
    event2 = Event.objects.create(
        organization=organization,
        name="Weekly Reading Circle",
        start=timezone.now(),
    )
    assert event2.slug.startswith("weekly-reading-circle-")
    assert len(event2.slug) == len("weekly-reading-circle-") + 5  # 5 char suffix


@pytest.mark.django_db
def test_event_slug_collision_different_organizations(
    organization: Organization, organization_owner_user: RevelUser
) -> None:
    """Test that same slug can exist in different organizations."""
    org2 = Organization.objects.create(
        name="Another Org",
        slug="another-org",
        owner=organization_owner_user,
    )

    event1 = Event.objects.create(
        organization=organization,
        name="Weekly Reading Circle",
        start=timezone.now(),
    )
    assert event1.slug == "weekly-reading-circle"

    # Same name in different org should get same slug (no collision)
    event2 = Event.objects.create(
        organization=org2,
        name="Weekly Reading Circle",
        start=timezone.now(),
    )
    assert event2.slug == "weekly-reading-circle"


@pytest.mark.django_db
def test_event_slug_preserved_on_update(organization: Organization) -> None:
    """Test that existing slug is preserved when updating an event."""
    event = Event.objects.create(
        organization=organization,
        name="Original Name",
        start=timezone.now(),
    )
    original_slug = event.slug

    # Update the name
    event.name = "New Name"
    event.save()

    # Slug should not change
    event.refresh_from_db()
    assert event.slug == original_slug


@pytest.mark.django_db
def test_event_explicit_slug_preserved(organization: Organization) -> None:
    """Test that explicitly provided slug is not overwritten."""
    event = Event.objects.create(
        organization=organization,
        name="My Event",
        slug="custom-slug",
        start=timezone.now(),
    )
    assert event.slug == "custom-slug"


@pytest.mark.django_db
def test_event_multiple_collisions(organization: Organization) -> None:
    """Test that multiple events with same name all get unique slugs."""
    events = []
    for i in range(5):
        event = Event.objects.create(
            organization=organization,
            name="Recurring Event",
            start=timezone.now(),
        )
        events.append(event)

    # All slugs should be unique
    slugs = [e.slug for e in events]
    assert len(slugs) == len(set(slugs))

    # First one should be base slug
    assert events[0].slug == "recurring-event"

    # Rest should have suffixes
    for event in events[1:]:
        assert event.slug.startswith("recurring-event-")
