"""Per-organization announcement mute is enforced at the delivery choke point (#1031).

``_deliver_to_recipients`` is shared by the manual send, the scheduled sweep, and the
resend-to-new-signups flow, so every path must skip users who muted the organization,
while ``get_recipients`` (eligibility to *read* the announcement) stays unchanged.
"""

import datetime as dt
import typing as t

import pytest
from django.utils import timezone

from accounts.models import RevelUser
from conftest import RevelUserFactory
from events.models import (
    Announcement,
    Event,
    MembershipTier,
    Organization,
    OrganizationMember,
    OrganizationStaff,
    Ticket,
    TicketTier,
)
from events.service import announcement_service
from events.tasks import resend_announcements_to_new_signups, send_scheduled_announcements
from notifications.enums import NotificationType
from notifications.models import Notification

pytestmark = pytest.mark.django_db


@pytest.fixture
def org(revel_user_factory: RevelUserFactory) -> Organization:
    owner = revel_user_factory(username="mute_owner")
    return Organization.objects.create(name="Mute Org", slug="mute-org", owner=owner)


@pytest.fixture
def event(org: Organization) -> Event:
    return Event.objects.create(
        organization=org,
        name="Mute Event",
        slug="mute-event",
        event_type=Event.EventType.PUBLIC,
        visibility=Event.Visibility.PUBLIC,
        status=Event.EventStatus.OPEN,
        start=timezone.now() + dt.timedelta(days=2),
        end=timezone.now() + dt.timedelta(days=2, hours=3),
    )


def _mute(user: RevelUser, org: Organization) -> None:
    user.notification_preferences.muted_organizations.add(org)


def _ticket(event: Event, user: RevelUser) -> None:
    tier = TicketTier.objects.create(event=event, name=f"GA-{user.username}", price=0)
    Ticket.objects.create(
        event=event, tier=tier, user=user, status=Ticket.TicketStatus.ACTIVE, guest_name=user.get_display_name()
    )


def _notified_ids(announcement: Announcement) -> set[t.Any]:
    return set(
        Notification.objects.filter(
            notification_type=NotificationType.ORG_ANNOUNCEMENT,
            context__announcement_id=str(announcement.id),
        ).values_list("user_id", flat=True)
    )


def _announcement(org: Organization, **kwargs: t.Any) -> Announcement:
    tiers = kwargs.pop("target_tiers", None)
    announcement = Announcement.objects.create(
        organization=org, title="Hello", body="World", created_by=org.owner, **kwargs
    )
    if tiers:
        announcement.target_tiers.set(tiers)
    return announcement


class TestSendSkipsMutedUsers:
    def test_event_attendees(self, org: Organization, event: Event, revel_user_factory: RevelUserFactory) -> None:
        muted, unmuted = revel_user_factory(username="att_muted"), revel_user_factory(username="att_ok")
        _ticket(event, muted)
        _ticket(event, unmuted)
        _mute(muted, org)
        announcement = _announcement(org, event=event)

        sent = announcement_service.send_announcement(announcement)

        announcement.refresh_from_db()
        assert sent == 1
        assert announcement.recipient_count == 1
        assert _notified_ids(announcement) == {unmuted.id}

    def test_all_members(self, org: Organization, revel_user_factory: RevelUserFactory) -> None:
        muted, unmuted = revel_user_factory(username="mem_muted"), revel_user_factory(username="mem_ok")
        for user in (muted, unmuted):
            OrganizationMember.objects.create(organization=org, user=user)
        _mute(muted, org)
        announcement = _announcement(org, target_all_members=True)

        announcement_service.send_announcement(announcement)

        assert _notified_ids(announcement) == {unmuted.id}

    def test_tiers(self, org: Organization, revel_user_factory: RevelUserFactory) -> None:
        tier = MembershipTier.objects.create(organization=org, name="Gold")
        muted, unmuted = revel_user_factory(username="tier_muted"), revel_user_factory(username="tier_ok")
        for user in (muted, unmuted):
            OrganizationMember.objects.create(organization=org, user=user, tier=tier)
        _mute(muted, org)
        announcement = _announcement(org, target_tiers=[tier])

        announcement_service.send_announcement(announcement)

        assert _notified_ids(announcement) == {unmuted.id}

    def test_staff(self, org: Organization, revel_user_factory: RevelUserFactory) -> None:
        muted, unmuted = revel_user_factory(username="staff_muted"), revel_user_factory(username="staff_ok")
        for user in (muted, unmuted):
            OrganizationStaff.objects.create(organization=org, user=user)
        _mute(muted, org)
        announcement = _announcement(org, target_staff_only=True)

        announcement_service.send_announcement(announcement)

        assert _notified_ids(announcement) == {unmuted.id}

    def test_mute_of_another_org_does_not_apply(
        self, org: Organization, event: Event, revel_user_factory: RevelUserFactory
    ) -> None:
        other = Organization.objects.create(name="Other", slug="other-mute-org", owner=org.owner)
        user = revel_user_factory(username="other_muter")
        _ticket(event, user)
        _mute(user, other)
        announcement = _announcement(org, event=event)

        announcement_service.send_announcement(announcement)

        assert _notified_ids(announcement) == {user.id}

    def test_everyone_muted_sends_nothing(
        self, org: Organization, event: Event, revel_user_factory: RevelUserFactory
    ) -> None:
        user = revel_user_factory(username="only_muted")
        _ticket(event, user)
        _mute(user, org)
        announcement = _announcement(org, event=event)

        assert announcement_service.send_announcement(announcement) == 0

        announcement.refresh_from_db()
        assert announcement.status == Announcement.AnnouncementStatus.SENT
        assert announcement.recipient_count == 0
        assert _notified_ids(announcement) == set()

    def test_muted_user_can_still_read_with_past_visibility(
        self, org: Organization, event: Event, revel_user_factory: RevelUserFactory
    ) -> None:
        """Muting stops delivery, not eligibility: the org page still shows the announcement."""
        user = revel_user_factory(username="reader_muted")
        _ticket(event, user)
        _mute(user, org)
        announcement = _announcement(org, event=event, past_visibility=True)
        announcement_service.send_announcement(announcement)
        announcement.refresh_from_db()

        assert announcement_service.get_recipients(announcement).filter(id=user.id).exists()
        assert announcement_service.is_user_eligible_for_announcement(announcement, user)

    def test_recipient_count_preview_excludes_muted(
        self, org: Organization, event: Event, revel_user_factory: RevelUserFactory
    ) -> None:
        muted, unmuted = revel_user_factory(username="prev_muted"), revel_user_factory(username="prev_ok")
        _ticket(event, muted)
        _ticket(event, unmuted)
        _mute(muted, org)

        assert announcement_service.get_recipient_count(_announcement(org, event=event)) == 1


class TestScheduledAndResendSkipMutedUsers:
    def test_scheduled_sweep(
        self,
        org: Organization,
        event: Event,
        revel_user_factory: RevelUserFactory,
        django_capture_on_commit_callbacks: t.Any,
    ) -> None:
        muted, unmuted = revel_user_factory(username="sched_muted"), revel_user_factory(username="sched_ok")
        _ticket(event, muted)
        _ticket(event, unmuted)
        _mute(muted, org)
        announcement = _announcement(
            org,
            event=event,
            status=Announcement.AnnouncementStatus.SCHEDULED,
            scheduled_at=timezone.now() - dt.timedelta(minutes=1),
        )

        with django_capture_on_commit_callbacks(execute=True):
            send_scheduled_announcements()

        announcement.refresh_from_db()
        assert announcement.status == Announcement.AnnouncementStatus.SENT
        assert announcement.recipient_count == 1
        assert _notified_ids(announcement) == {unmuted.id}

    def test_resend_skips_muted_new_signup_without_looping(
        self,
        org: Organization,
        event: Event,
        revel_user_factory: RevelUserFactory,
        django_capture_on_commit_callbacks: t.Any,
    ) -> None:
        """A muted new sign-up is skipped on every sweep and never inflates recipient_count."""
        original = revel_user_factory(username="resend_original")
        _ticket(event, original)
        announcement = _announcement(org, event=event, resend_to_new_signups=True)
        announcement_service.send_announcement(announcement)

        muted_new, unmuted_new = revel_user_factory(username="resend_muted"), revel_user_factory(username="resend_ok")
        _ticket(event, muted_new)
        _ticket(event, unmuted_new)
        _mute(muted_new, org)

        assert announcement_service.resend_to_new_recipients(announcement) == 1
        # Second sweep: the muted user is still in the delta but nothing is delivered.
        with django_capture_on_commit_callbacks(execute=True):
            resend_announcements_to_new_signups()
        assert announcement_service.resend_to_new_recipients(announcement) == 0

        announcement.refresh_from_db()
        assert announcement.recipient_count == 2
        assert _notified_ids(announcement) == {original.id, unmuted_new.id}
