"""Model-level behavior of ``Event.visibility_settings`` (#792).

Covers the parsed-flags accessor, the staff bypass, the always-public
``is_full`` boolean, the guest-list AND semantics, and the copy of the field
onto recurring occurrences.
"""

import typing as t
from datetime import timedelta

import pytest
from django.contrib.auth.models import AnonymousUser
from django.core.exceptions import ValidationError as DjangoValidationError
from django.utils import timezone
from pydantic import BaseModel
from pydantic import ValidationError as PydanticValidationError

from accounts.models import RevelUser
from events.models import (
    AttendeeVisibilityFlag,
    Event,
    EventRSVP,
    Organization,
    OrganizationStaff,
)
from events.schema import EventEditSchema
from events.schema.recurring_event import TemplateEditSchema
from events.service.duplication import _EXCLUDED_FROM_COPY, duplicate_event
from events.service.event_update_service import _field_changed
from events.utils.visibility_settings import (
    ResourceVisibility,
    build_visibility_settings_update,
    validate_visibility_settings,
)

pytestmark = pytest.mark.django_db

HIDE_ALL: dict[str, t.Any] = {
    "show_attendee_count": False,
    "show_capacity": False,
    "show_attendee_list": False,
}


def test_defaults_reproduce_current_behavior() -> None:
    """An untouched event stores ``{}`` and every toggle reads as visible."""
    flags = validate_visibility_settings({})

    assert flags.show_attendee_count is True
    assert flags.show_capacity is True
    assert flags.show_attendee_list is True


def test_none_parses_to_defaults() -> None:
    """A null blob is tolerated and yields the all-visible defaults."""
    assert validate_visibility_settings(None).show_capacity is True


def test_partial_blob_fills_missing_toggles(public_event: Event) -> None:
    """Storing one toggle leaves the others at their visible default."""
    public_event.visibility_settings = {"show_capacity": False}

    flags = public_event.visibility_flags

    assert flags.show_capacity is False
    assert flags.show_attendee_count is True
    assert flags.show_attendee_list is True


def test_unknown_key_is_rejected_by_clean(public_event: Event) -> None:
    """``extra='forbid'`` surfaces as a field error, guarding the admin JSON widget."""
    public_event.visibility_settings = {"show_everything": True}

    with pytest.raises(DjangoValidationError) as exc_info:
        public_event.full_clean()

    assert "visibility_settings" in exc_info.value.message_dict


def test_wrong_type_is_rejected_by_clean(public_event: Event) -> None:
    """A non-boolean toggle is rejected rather than silently coerced."""
    public_event.visibility_settings = {"show_capacity": "maybe"}

    with pytest.raises(DjangoValidationError) as exc_info:
        public_event.full_clean()

    assert "visibility_settings" in exc_info.value.message_dict


def test_visibility_flags_are_not_cached_across_assignment(public_event: Event) -> None:
    """Reassigning the raw blob must not serve stale flags."""
    assert public_event.visibility_flags.show_capacity is True

    public_event.visibility_settings = {"show_capacity": False}

    assert public_event.visibility_flags.show_capacity is False


class TestBuildUpdate:
    """``build_visibility_settings_update`` merges rather than replaces.

    Exercised against the two real edit schemas rather than a stand-in, so the
    tests cannot drift from the nullability the endpoints actually declare.
    """

    @pytest.mark.parametrize("schema", [EventEditSchema, TemplateEditSchema])
    def test_absent_field_yields_no_write(self, schema: type[BaseModel]) -> None:
        payload = schema.model_validate({})

        assert build_visibility_settings_update({"show_capacity": False}, payload) == {}

    def test_event_edit_schema_rejects_an_explicit_null(self) -> None:
        """The field is non-nullable there, so a null never reaches the helper."""
        with pytest.raises(PydanticValidationError):
            EventEditSchema.model_validate({"visibility_settings": None})

    def test_template_edit_schema_treats_an_explicit_null_as_no_change(self) -> None:
        """It declares ``| None = None`` like every sibling, so a null does arrive.

        Writing it through would violate the column's NOT NULL, so it must
        produce no write at all.
        """
        payload = TemplateEditSchema.model_validate({"visibility_settings": None})

        assert build_visibility_settings_update({"show_capacity": False}, payload) == {}

    def test_partial_send_preserves_untouched_toggles(self) -> None:
        payload = EventEditSchema.model_validate({"visibility_settings": {"show_capacity": False}})

        assert build_visibility_settings_update({"show_attendee_count": False}, payload) == {
            "visibility_settings": {"show_attendee_count": False, "show_capacity": False},
        }

    def test_sent_toggle_wins_over_stored(self) -> None:
        payload = EventEditSchema.model_validate({"visibility_settings": {"show_capacity": True}})

        assert build_visibility_settings_update({"show_capacity": False}, payload) == {
            "visibility_settings": {"show_capacity": True},
        }

    def test_none_stored_is_tolerated(self) -> None:
        payload = EventEditSchema.model_validate({"visibility_settings": {"show_capacity": False}})

        assert build_visibility_settings_update(None, payload) == {
            "visibility_settings": {"show_capacity": False},
        }

    def test_stored_blob_is_not_mutated(self) -> None:
        stored: dict[str, t.Any] = {"show_attendee_count": False}
        payload = EventEditSchema.model_validate({"visibility_settings": {"show_capacity": False}})

        build_visibility_settings_update(stored, payload)

        assert stored == {"show_attendee_count": False}


class TestOccurrenceDiffNormalisation:
    """A default-equivalent visibility write must not detach an occurrence."""

    def test_empty_and_explicit_defaults_compare_equal(self) -> None:
        assert not _field_changed("visibility_settings", {}, {"show_capacity": True})

    def test_a_real_visibility_change_is_detected(self) -> None:
        assert _field_changed("visibility_settings", {}, {"show_capacity": False})

    def test_other_fields_still_use_plain_inequality(self) -> None:
        assert _field_changed("name", "Old", "New")
        assert not _field_changed("name", "Same", "Same")


class TestBypass:
    """Who sees the real numbers regardless of the toggles."""

    def test_anonymous_does_not_bypass(self, public_event: Event) -> None:
        assert public_event.bypasses_visibility_settings(AnonymousUser()) is False

    def test_unrelated_user_does_not_bypass(self, public_event: Event, nonmember_user: RevelUser) -> None:
        assert public_event.bypasses_visibility_settings(nonmember_user) is False

    def test_owner_bypasses(self, public_event: Event, organization_owner_user: RevelUser) -> None:
        assert public_event.bypasses_visibility_settings(organization_owner_user) is True

    def test_org_staff_bypasses(
        self,
        public_event: Event,
        organization_staff_user: RevelUser,
        staff_member: OrganizationStaff,
    ) -> None:
        assert public_event.bypasses_visibility_settings(organization_staff_user) is True

    def test_superuser_bypasses(self, public_event: Event, superuser: RevelUser) -> None:
        assert public_event.bypasses_visibility_settings(superuser) is True

    def test_result_is_cached_per_user(self, public_event: Event, nonmember_user: RevelUser) -> None:
        """The second call is served from the instance cache."""
        assert public_event.bypasses_visibility_settings(nonmember_user) is False

        # Poison the cache: a cached False must survive an ownership change.
        public_event.organization.owner_id = nonmember_user.id

        assert public_event.bypasses_visibility_settings(nonmember_user) is False


class TestPredicates:
    """Each toggle gates exactly its own predicate."""

    def test_all_visible_by_default(self, public_event: Event, nonmember_user: RevelUser) -> None:
        assert public_event.can_user_see_attendee_count(nonmember_user) is True
        assert public_event.can_user_see_capacity(nonmember_user) is True
        assert public_event.can_user_see_attendee_list(nonmember_user) is True

    def test_toggles_are_independent(self, public_event: Event, nonmember_user: RevelUser) -> None:
        public_event.visibility_settings = {"show_attendee_count": False}

        assert public_event.can_user_see_attendee_count(nonmember_user) is False
        assert public_event.can_user_see_capacity(nonmember_user) is True
        assert public_event.can_user_see_attendee_list(nonmember_user) is True

    def test_hidden_from_anonymous(self, public_event: Event) -> None:
        public_event.visibility_settings = HIDE_ALL
        anon = AnonymousUser()

        assert public_event.can_user_see_attendee_count(anon) is False
        assert public_event.can_user_see_capacity(anon) is False
        assert public_event.can_user_see_attendee_list(anon) is False

    def test_owner_still_sees_everything(self, public_event: Event, organization_owner_user: RevelUser) -> None:
        public_event.visibility_settings = HIDE_ALL

        assert public_event.can_user_see_attendee_count(organization_owner_user) is True
        assert public_event.can_user_see_capacity(organization_owner_user) is True
        assert public_event.can_user_see_attendee_list(organization_owner_user) is True


class TestIsFull:
    """``is_full`` stays truthful — and public — regardless of the toggles."""

    def test_false_below_capacity(self, public_event: Event) -> None:
        public_event.attendee_count = 9  # max_attendees == 10

        assert public_event.is_full is False

    def test_true_at_capacity(self, public_event: Event) -> None:
        public_event.attendee_count = 10

        assert public_event.is_full is True

    def test_true_above_capacity(self, public_event: Event) -> None:
        """Overbooked events must still read as full."""
        public_event.attendee_count = 11

        assert public_event.is_full is True

    def test_false_when_uncapped(self, event: Event) -> None:
        """``max_attendees == 0`` means unlimited, never full."""
        event.max_attendees = 0
        event.attendee_count = 999

        assert event.is_full is False

    def test_unaffected_by_hidden_counts(self, public_event: Event) -> None:
        public_event.visibility_settings = HIDE_ALL
        public_event.attendee_count = 10

        assert public_event.is_full is True


class TestAttendeeListAndSemantics:
    """``show_attendee_list`` ANDs with the per-user visibility matrix."""

    @pytest.fixture
    def opted_in_attendee(self, public_event: Event, nonmember_user: RevelUser, member_user: RevelUser) -> RevelUser:
        """``member_user`` attends and is flagged visible to ``nonmember_user``."""
        EventRSVP.objects.create(user=member_user, event=public_event, status=EventRSVP.RsvpStatus.YES)
        AttendeeVisibilityFlag.objects.create(
            user=nonmember_user, event=public_event, target=member_user, is_visible=True
        )
        return member_user

    def test_opt_in_plus_event_show_is_visible(
        self, public_event: Event, nonmember_user: RevelUser, opted_in_attendee: RevelUser
    ) -> None:
        assert list(public_event.attendees(nonmember_user)) == [opted_in_attendee]

    def test_opt_in_plus_event_hide_is_hidden(
        self, public_event: Event, nonmember_user: RevelUser, opted_in_attendee: RevelUser
    ) -> None:
        """The event toggle overrides a user who opted in."""
        public_event.visibility_settings = {"show_attendee_list": False}

        assert list(public_event.attendees(nonmember_user)) == []

    def test_opt_out_plus_event_show_stays_hidden(
        self, public_event: Event, nonmember_user: RevelUser, member_user: RevelUser
    ) -> None:
        """No visibility flag means opted out; the event toggle cannot reveal them."""
        EventRSVP.objects.create(user=member_user, event=public_event, status=EventRSVP.RsvpStatus.YES)

        assert list(public_event.attendees(nonmember_user)) == []

    def test_staff_see_everyone_even_when_hidden(
        self,
        public_event: Event,
        member_user: RevelUser,
        organization_owner_user: RevelUser,
    ) -> None:
        public_event.visibility_settings = {"show_attendee_list": False}
        EventRSVP.objects.create(user=member_user, event=public_event, status=EventRSVP.RsvpStatus.YES)

        assert list(public_event.attendees(organization_owner_user)) == [member_user]


class TestOccurrenceCopy:
    """``visibility_settings`` rides along to duplicates and occurrences."""

    def test_not_excluded_from_copy(self) -> None:
        """A regression guard: adding it to the exclusion set would break the feature."""
        assert "visibility_settings" not in _EXCLUDED_FROM_COPY

    def test_duplicate_carries_the_settings(self, public_event: Event) -> None:
        public_event.visibility_settings = HIDE_ALL
        public_event.save(update_fields=["visibility_settings"])

        duplicate = duplicate_event(
            template_event=public_event,
            new_name="Copy",
            new_start=public_event.start + timedelta(days=7),
        )

        assert duplicate.visibility_settings == HIDE_ALL
        assert duplicate.visibility_flags.show_capacity is False

    def test_materialized_occurrence_carries_the_settings(
        self, organization: Organization, organization_owner_user: RevelUser
    ) -> None:
        from events.models import EventSeries
        from events.service.recurrence_service import materialize_occurrence

        template = Event.objects.create(
            organization=organization,
            name="Weekly",
            status=Event.EventStatus.DRAFT,
            start=timezone.now() + timedelta(days=1),
            end=timezone.now() + timedelta(days=1, hours=2),
            is_template=True,
            visibility_settings=HIDE_ALL,
        )
        series = EventSeries.objects.create(organization=organization, name="Weekly Series", template_event=template)

        occurrence = materialize_occurrence(series, template.start + timedelta(days=7), index=1)

        assert occurrence.visibility_settings == HIDE_ALL

    def test_propagatable_fields_includes_the_setting(self) -> None:
        """Editing the template's toggles must reach unmodified future occurrences."""
        from events.service.recurrence_service import PROPAGATABLE_FIELDS

        assert "visibility_settings" in PROPAGATABLE_FIELDS


class TestPhase2Fields:
    """``address_visibility`` and ``show_pronoun_distribution`` on the blob (#793)."""

    def test_defaults_mirror_the_old_column_defaults(self) -> None:
        """An untouched blob reproduces pre-#793 behavior exactly."""
        flags = validate_visibility_settings({})
        assert flags.address_visibility == ResourceVisibility.PUBLIC
        assert flags.show_pronoun_distribution is False

    def test_address_visibility_accepts_every_enum_member(self) -> None:
        """Every ResourceVisibility value round-trips through the blob."""
        for member in ResourceVisibility:
            flags = validate_visibility_settings({"address_visibility": member.value})
            assert flags.address_visibility == member

    def test_address_visibility_rejects_unknown_values(self) -> None:
        """A bogus visibility is a validation error, not a silent PUBLIC."""
        with pytest.raises(PydanticValidationError):
            validate_visibility_settings({"address_visibility": "everyone-ish"})

    def test_reexport_is_the_same_object(self) -> None:
        """``events.models.mixins`` re-exports the canonical enum, not a copy."""
        from events.models.mixins import ResourceVisibility as ReExported
        from events.utils.visibility_settings import ResourceVisibility as Canonical

        assert ReExported is Canonical
