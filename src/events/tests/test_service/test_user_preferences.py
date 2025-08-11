import pytest

from accounts.models import RevelUser
from conftest import RevelUserFactory
from events import models
from events.models import OrganizationMember, OrganizationStaff
from events.schema import BaseUserPreferencesSchema
from events.service.user_preferences_service import resolve_visibility, set_preferences

pytestmark = pytest.mark.django_db


@pytest.fixture
def viewer(revel_user_factory: RevelUserFactory) -> RevelUser:
    return revel_user_factory()


@pytest.fixture
def target(revel_user_factory: RevelUserFactory) -> RevelUser:
    return revel_user_factory()


class TestResolveVisibility:
    def test_owner_can_always_see(
        self, organization_owner_user: RevelUser, target: RevelUser, event: models.Event
    ) -> None:
        """Test that the organization owner can see any attendee."""
        target.general_preferences.show_me_on_attendee_list = "never"
        target.general_preferences.save()
        assert (
            resolve_visibility(
                viewer=organization_owner_user,
                target=target,
                event=event,
                owner_id=organization_owner_user.id,
                staff_ids=set(),
            )
            is True
        )

    def test_staff_can_always_see(
        self,
        organization_staff_user: RevelUser,
        target: RevelUser,
        event: models.Event,
        staff_member: OrganizationStaff,
    ) -> None:
        """Test that a staff member can see any attendee."""
        target.general_preferences.show_me_on_attendee_list = "never"
        target.general_preferences.save()
        assert (
            resolve_visibility(
                viewer=organization_staff_user,
                target=target,
                event=event,
                owner_id=event.organization.owner_id,
                staff_ids={organization_staff_user.id},
            )
            is True
        )

    def test_target_preference_always(self, viewer: RevelUser, target: RevelUser, event: models.Event) -> None:
        """Test 'always' preference makes the target visible."""
        target.general_preferences.show_me_on_attendee_list = "always"
        target.general_preferences.save()
        assert (
            resolve_visibility(
                viewer=viewer,
                target=target,
                event=event,
                owner_id=event.organization.owner_id,
                staff_ids=set(),
            )
            is True
        )

    def test_target_preference_never(self, viewer: RevelUser, target: RevelUser, event: models.Event) -> None:
        """Test 'never' preference makes the target invisible."""
        target.general_preferences.show_me_on_attendee_list = "never"
        target.general_preferences.save()
        assert (
            resolve_visibility(
                viewer=viewer,
                target=target,
                event=event,
                owner_id=event.organization.owner_id,
                staff_ids=set(),
            )
            is False
        )

    def test_preference_to_members(
        self, viewer: RevelUser, target: RevelUser, event: models.Event, organization_membership: OrganizationMember
    ) -> None:
        """Test 'to_members' preference visibility."""
        target.general_preferences.show_me_on_attendee_list = "to_members"
        target.general_preferences.save()

        # Both are members
        models.OrganizationMember.objects.create(organization=event.organization, user=viewer)
        models.OrganizationMember.objects.create(organization=event.organization, user=target)
        assert (
            resolve_visibility(
                viewer=viewer,
                target=target,
                event=event,
                owner_id=event.organization.owner_id,
                staff_ids=set(),
            )
            is True
        )

        # Viewer is not a member
        viewer.organization_memberships.all().delete()
        assert (
            resolve_visibility(
                viewer=viewer,
                target=target,
                event=event,
                owner_id=event.organization.owner_id,
                staff_ids=set(),
            )
            is False
        )

    def test_preference_to_invitees(self, viewer: RevelUser, target: RevelUser, event: models.Event) -> None:
        """Test 'to_invitees' preference visibility."""
        target.general_preferences.show_me_on_attendee_list = "to_invitees"
        target.general_preferences.save()

        # Viewer is an attendee (via ticket)
        tier = event.ticket_tiers.first()
        assert tier is not None
        models.Ticket.objects.create(event=event, user=viewer, tier=tier)
        assert (
            resolve_visibility(
                viewer=viewer,
                target=target,
                event=event,
                owner_id=event.organization.owner_id,
                staff_ids=set(),
            )
            is True
        )

        # Viewer is not an attendee
        viewer.tickets.all().delete()
        assert (
            resolve_visibility(
                viewer=viewer,
                target=target,
                event=event,
                owner_id=event.organization.owner_id,
                staff_ids=set(),
            )
            is False
        )

    def test_preference_event_override(self, viewer: RevelUser, target: RevelUser, event: models.Event) -> None:
        """Test that event-specific preferences override global preferences."""
        # Global pref is 'never'
        target.general_preferences.show_me_on_attendee_list = "never"
        target.general_preferences.save()
        # Event pref is 'always'
        models.UserEventPreferences.objects.create(event=event, user=target, show_me_on_attendee_list="always")

        assert (
            resolve_visibility(
                viewer=viewer,
                target=target,
                event=event,
                owner_id=event.organization.owner_id,
                staff_ids=set(),
            )
            is True
        )


class TestSetPreferences:
    def test_set_preferences_cascade_overwrite(
        self,
        member_user: RevelUser,
        organization: models.Organization,
        event_series: models.EventSeries,
        event: models.Event,
    ) -> None:
        """Test that parent preferences override children preferences in a cascading manner."""
        # Set up
        assert member_user.general_preferences.silence_all_notifications is False
        org_prefs = models.UserOrganizationPreferences.objects.create(user=member_user, organization=organization)
        assert org_prefs.silence_all_notifications is False
        ser_prefs = models.UserEventSeriesPreferences.objects.create(user=member_user, event_series=event_series)
        assert ser_prefs.silence_all_notifications is False
        evt_prefs = models.UserEventPreferences.objects.create(user=member_user, event=event)
        assert evt_prefs.silence_all_notifications is False

        # Act
        payload = BaseUserPreferencesSchema(silence_all_notifications=True)
        set_preferences(member_user.general_preferences, payload, overwrite_children=True)

        # Assert
        member_user.refresh_from_db()
        assert member_user.general_preferences.silence_all_notifications is True
        org_prefs.refresh_from_db()  # type: ignore[unreachable]
        assert org_prefs.silence_all_notifications is True
        ser_prefs.refresh_from_db()
        assert ser_prefs.silence_all_notifications is True
        evt_prefs.refresh_from_db()
        assert evt_prefs.silence_all_notifications is True
