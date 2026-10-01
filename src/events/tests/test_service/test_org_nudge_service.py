"""Tests for the org setup nudge planner and sender (events.service.org_nudge_service)."""

import datetime
import typing as t
from unittest.mock import MagicMock, patch

import pytest
from django.db import IntegrityError
from django.utils import timezone

from accounts.models import RevelUser
from events.models import Event, Organization, OrganizationNudge
from events.service import org_nudge_service
from notifications.enums import NotificationType
from notifications.models import EmailSuppression, NotificationPreference

pytestmark = pytest.mark.django_db

Trigger = OrganizationNudge.Trigger
NOW = timezone.now()


def _backdate(obj: Organization | Event, days: int, *, updated: bool = False) -> None:
    """Move created_at (and optionally updated_at) into the past; auto_now fields need .update()."""
    when = NOW - datetime.timedelta(days=days)
    fields: dict[str, t.Any] = {"created_at": when}
    if updated:
        fields["updated_at"] = when
    type(obj).objects.filter(pk=obj.pk).update(**fields)
    obj.refresh_from_db()


@pytest.fixture
def owner(user: RevelUser) -> RevelUser:
    user.email_verified = True
    user.save(update_fields=["email_verified"])
    return user


@pytest.fixture
def stalled_org(owner: RevelUser) -> Organization:
    """Three weeks old, default (PRIVATE) visibility, no events."""
    org = Organization.objects.create(name="Stalled", slug="stalled", owner=owner)
    _backdate(org, 21)
    return org


def _make_event(org: Organization, *, status: str, start_in_days: int, **kwargs: t.Any) -> Event:
    start = NOW + datetime.timedelta(days=start_in_days)
    return Event.objects.create(
        organization=org,
        name=kwargs.pop("name", f"Event {status} {start_in_days}"),
        status=status,
        start=start,
        end=start + datetime.timedelta(hours=3),
        **kwargs,
    )


def _plan(org: Organization, now: datetime.datetime = NOW) -> org_nudge_service.PlannedNudge | None:
    plans = org_nudge_service.plan_nudges(now=now, organization=org)
    return plans[0] if plans else None


def _record(org: Organization, trigger: str, sequence: int, *, days_ago: int, episode_key: str = "") -> None:
    nudge = OrganizationNudge.objects.create(
        organization=org, trigger=trigger, sequence=sequence, episode_key=episode_key
    )
    OrganizationNudge.objects.filter(pk=nudge.pk).update(created_at=NOW - datetime.timedelta(days=days_ago))


# --- Eligibility ---


def test_new_org_inside_grace_period_is_not_nudged(owner: RevelUser) -> None:
    org = Organization.objects.create(name="Fresh", slug="fresh", owner=owner)
    _backdate(org, 3)
    assert _plan(org) is None


@pytest.mark.parametrize(
    "field,value",
    [("email_verified", False), ("guest", True), ("is_active", False)],
)
def test_unreachable_owner_is_not_nudged(stalled_org: Organization, owner: RevelUser, field: str, value: bool) -> None:
    setattr(owner, field, value)
    owner.save(update_fields=[field])
    assert _plan(stalled_org) is None


def test_owner_who_opted_out_is_skipped_without_burning_a_cap(stalled_org: Organization, owner: RevelUser) -> None:
    prefs = NotificationPreference.objects.get(user=owner)
    prefs.disable_email_for_type(NotificationType.ORG_SETUP_NUDGE)
    prefs.save()

    assert _plan(stalled_org) is None
    assert org_nudge_service.send_nudges(now=NOW, organization=stalled_org) == []
    assert not OrganizationNudge.objects.exists()


def test_silenced_owner_is_skipped(stalled_org: Organization, owner: RevelUser) -> None:
    NotificationPreference.objects.filter(user=owner).update(silence_all_notifications=True)
    assert _plan(stalled_org) is None


def test_suppressed_owner_address_is_skipped(stalled_org: Organization, owner: RevelUser) -> None:
    EmailSuppression.objects.create(
        email=owner.email, reason=EmailSuppression.Reason.HARD_BOUNCE, source=EmailSuppression.Source.PROVIDER
    )
    assert _plan(stalled_org) is None


# --- Rules ---


@pytest.mark.parametrize(
    "visibility,expected",
    [
        (Organization.Visibility.PRIVATE, Trigger.PRIVATE_PROFILE),
        (Organization.Visibility.STAFF_ONLY, Trigger.PRIVATE_PROFILE),
        (Organization.Visibility.MEMBERS_ONLY, Trigger.NO_EVENTS),
        (Organization.Visibility.UNLISTED, Trigger.NO_EVENTS),
        (Organization.Visibility.PUBLIC, Trigger.NO_EVENTS),
    ],
)
def test_private_profile_only_for_private_and_staff_only(
    stalled_org: Organization, visibility: str, expected: str
) -> None:
    """MEMBERS_ONLY / UNLISTED are deliberate choices: they fall through to the next rule."""
    Organization.objects.filter(pk=stalled_org.pk).update(visibility=visibility)
    plan = _plan(stalled_org)
    assert plan is not None
    assert plan.trigger == expected


def test_stale_future_draft_wins_over_private_profile(stalled_org: Organization) -> None:
    old = _make_event(stalled_org, status=Event.EventStatus.DRAFT, start_in_days=30, name="Older draft")
    _backdate(old, 40, updated=True)
    recent = _make_event(stalled_org, status=Event.EventStatus.DRAFT, start_in_days=30, name="Recent draft")
    _backdate(recent, 20, updated=True)

    plan = _plan(stalled_org)

    assert plan is not None
    assert plan.trigger == Trigger.DRAFT_EVENT
    assert plan.target_event == recent  # the most recently edited stale draft


@pytest.mark.parametrize(
    "start_in_days,updated_days_ago,is_template",
    [
        (30, 3, False),  # edited recently
        (-5, 30, False),  # start already passed
        (30, 30, True),  # recurring-series template
    ],
)
def test_draft_rule_ignores_fresh_past_and_template_drafts(
    stalled_org: Organization, start_in_days: int, updated_days_ago: int, is_template: bool
) -> None:
    Organization.objects.filter(pk=stalled_org.pk).update(visibility=Organization.Visibility.PUBLIC)
    draft = _make_event(
        stalled_org, status=Event.EventStatus.DRAFT, start_in_days=start_in_days, is_template=is_template
    )
    _backdate(draft, updated_days_ago, updated=True)

    plan = _plan(stalled_org)

    assert plan is None or plan.trigger != Trigger.DRAFT_EVENT


def test_no_events_needs_fourteen_days(owner: RevelUser) -> None:
    org = Organization.objects.create(
        name="Young", slug="young", owner=owner, visibility=Organization.Visibility.PUBLIC
    )
    _backdate(org, 10)
    assert _plan(org) is None
    _backdate(org, 15)
    plan = _plan(org)
    assert plan is not None
    assert plan.trigger == Trigger.NO_EVENTS


def test_no_events_does_not_fire_when_a_draft_exists(stalled_org: Organization) -> None:
    Organization.objects.filter(pk=stalled_org.pk).update(visibility=Organization.Visibility.PUBLIC)
    _make_event(stalled_org, status=Event.EventStatus.DRAFT, start_in_days=30)  # fresh draft
    assert _plan(stalled_org) is None


# --- Caps and spacing ---


def test_spacing_blocks_a_second_nudge_within_fourteen_days(stalled_org: Organization) -> None:
    _record(stalled_org, Trigger.PRIVATE_PROFILE, 1, days_ago=10)
    assert _plan(stalled_org) is None


def test_second_nudge_is_marked_last_then_next_trigger(stalled_org: Organization) -> None:
    _record(stalled_org, Trigger.PRIVATE_PROFILE, 1, days_ago=15)
    plan = _plan(stalled_org)
    assert plan is not None
    assert (plan.trigger, plan.sequence, plan.is_last) == (Trigger.PRIVATE_PROFILE, 2, True)

    _record(stalled_org, Trigger.PRIVATE_PROFILE, 2, days_ago=15)
    plan = _plan(stalled_org)
    assert plan is not None
    assert (plan.trigger, plan.sequence, plan.is_last) == (Trigger.NO_EVENTS, 1, False)


def test_database_rejects_a_duplicate_sequence(stalled_org: Organization) -> None:
    OrganizationNudge.objects.create(organization=stalled_org, trigger=Trigger.NO_EVENTS, sequence=1)
    with pytest.raises(IntegrityError):
        OrganizationNudge.objects.bulk_create(
            [OrganizationNudge(organization=stalled_org, trigger=Trigger.NO_EVENTS, sequence=1)]
        )


# --- Check-in ---


def _exhaust_fixable_triggers(org: Organization) -> None:
    for trigger in (Trigger.PRIVATE_PROFILE, Trigger.NO_EVENTS):
        for sequence in (1, 2):
            _record(org, trigger, sequence, days_ago=20)


def test_check_in_after_fixable_triggers_are_exhausted(stalled_org: Organization, settings: t.Any) -> None:
    settings.ORG_NUDGE_REPLY_TO = "founder@example.com"
    _exhaust_fixable_triggers(stalled_org)

    plan = _plan(stalled_org)

    assert plan is not None
    assert (plan.trigger, plan.sequence, plan.is_last) == (Trigger.CHECK_IN, 1, True)


def test_check_in_needs_a_reply_to_address(stalled_org: Organization, settings: t.Any) -> None:
    settings.ORG_NUDGE_REPLY_TO = ""
    _exhaust_fixable_triggers(stalled_org)
    assert _plan(stalled_org) is None


def test_check_in_needs_a_previous_nudge(owner: RevelUser, settings: t.Any) -> None:
    """A fresh public org with only a recent draft matches nothing, and must not jump to the check-in."""
    settings.ORG_NUDGE_REPLY_TO = "founder@example.com"
    org = Organization.objects.create(name="Quiet", slug="quiet", owner=owner, visibility="public")
    _backdate(org, 21)
    _make_event(org, status=Event.EventStatus.DRAFT, start_in_days=30)
    assert _plan(org) is None


def test_check_in_skipped_once_something_was_published(stalled_org: Organization, settings: t.Any) -> None:
    settings.ORG_NUDGE_REPLY_TO = "founder@example.com"
    _exhaust_fixable_triggers(stalled_org)
    _make_event(stalled_org, status=Event.EventStatus.OPEN, start_in_days=10)
    assert _plan(stalled_org) is None


# --- Dormant ---


@pytest.fixture
def dormant_org(stalled_org: Organization) -> tuple[Organization, Event]:
    Organization.objects.filter(pk=stalled_org.pk).update(visibility=Organization.Visibility.PUBLIC)
    last = _make_event(stalled_org, status=Event.EventStatus.CLOSED, start_in_days=-120)
    return stalled_org, last


def test_dormant_after_ninety_quiet_days(dormant_org: tuple[Organization, Event]) -> None:
    org, last = dormant_org
    plan = _plan(org)
    assert plan is not None
    assert (plan.trigger, plan.episode_key, plan.target_event) == (Trigger.DORMANT, str(last.id), last)


def test_dormant_skipped_with_something_upcoming(dormant_org: tuple[Organization, Event]) -> None:
    org, _last = dormant_org
    _make_event(org, status=Event.EventStatus.DRAFT, start_in_days=30)  # fresh draft = planning
    assert _plan(org) is None


def test_dormant_rearms_for_a_new_quiet_episode(dormant_org: tuple[Organization, Event]) -> None:
    org, last = dormant_org
    _record(org, Trigger.DORMANT, 1, days_ago=60, episode_key=str(last.id))
    assert _plan(org) is None  # this episode's cap is spent

    newer = _make_event(org, status=Event.EventStatus.CLOSED, start_in_days=-100, name="Comeback")
    plan = _plan(org)
    assert plan is not None
    assert (plan.trigger, plan.episode_key) == (Trigger.DORMANT, str(newer.id))


# --- Sending ---


@patch("notifications.tasks.dispatch_notification.delay")
def test_send_writes_log_and_notification_and_dispatches_on_commit(
    mock_delay: MagicMock,
    stalled_org: Organization,
    owner: RevelUser,
    settings: t.Any,
    django_capture_on_commit_callbacks: t.Any,
) -> None:
    settings.ORG_NUDGE_REPLY_TO = "founder@example.com"

    with django_capture_on_commit_callbacks(execute=True):
        sent = org_nudge_service.send_nudges(now=NOW, organization=stalled_org)

    assert [p.trigger for p in sent] == [Trigger.PRIVATE_PROFILE]
    nudge = OrganizationNudge.objects.get()
    assert (nudge.trigger, nudge.sequence) == (Trigger.PRIVATE_PROFILE, 1)
    notification = nudge.notification
    assert notification is not None
    assert notification.user == owner
    assert notification.notification_type == NotificationType.ORG_SETUP_NUDGE
    assert notification.context["action_url"].endswith("/org/stalled/admin/settings")
    assert notification.context["can_reply"] is True
    assert notification.context["is_last"] is False
    mock_delay.assert_called_once_with(str(notification.id))


@patch("notifications.tasks.dispatch_notification.delay")
def test_send_replans_and_skips_an_org_with_nothing_left_to_fix(
    mock_delay: MagicMock, stalled_org: Organization
) -> None:
    """send_nudge re-plans under the lock instead of trusting an earlier plan."""
    Organization.objects.filter(pk=stalled_org.pk).update(visibility=Organization.Visibility.PUBLIC)
    _make_event(stalled_org, status=Event.EventStatus.OPEN, start_in_days=10)

    assert org_nudge_service.send_nudge(stalled_org, now=NOW) is None
    assert not OrganizationNudge.objects.exists()


@patch("notifications.tasks.dispatch_notification.delay")
def test_draft_nudge_links_to_the_draft_editor(mock_delay: MagicMock, stalled_org: Organization) -> None:
    draft = _make_event(stalled_org, status=Event.EventStatus.DRAFT, start_in_days=30, name="Summer party")
    _backdate(draft, 20, updated=True)

    org_nudge_service.send_nudge(stalled_org, now=NOW)

    nudge = OrganizationNudge.objects.get()
    assert nudge.target_event == draft
    assert nudge.notification is not None
    context = nudge.notification.context
    assert context["event_name"] == "Summer party"
    assert context["action_url"].endswith(f"/org/stalled/admin/events/{draft.id}/edit")
