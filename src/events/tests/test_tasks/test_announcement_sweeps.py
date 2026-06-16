"""Tests for the scheduled-send and resend-to-new-signups beat sweeps."""

import datetime as dt
import typing as t

import pytest
from django.utils import timezone

from accounts.models import RevelUser
from conftest import RevelUserFactory
from events.announcement_tasks import resend_announcements_to_new_signups, send_scheduled_announcements
from events.models import Announcement, Event, Organization, Ticket, TicketTier

pytestmark = pytest.mark.django_db


@pytest.fixture
def org(revel_user_factory: RevelUserFactory) -> Organization:
    owner = revel_user_factory(username="sweep_owner")
    return Organization.objects.create(name="Sweep Org", slug="sweep-org", owner=owner)


@pytest.fixture
def event(org: Organization) -> Event:
    return Event.objects.create(
        organization=org,
        name="Sweep Event",
        slug="sweep-event",
        event_type=Event.EventType.PUBLIC,
        visibility=Event.Visibility.PUBLIC,
        status=Event.EventStatus.OPEN,
        start=timezone.now() + dt.timedelta(days=2),
        end=timezone.now() + dt.timedelta(days=2, hours=3),
    )


def _ticket(event: Event, user: RevelUser) -> None:
    # TicketTier has a unique (event, name) constraint; Ticket.guest_name is required.
    tier = TicketTier.objects.create(event=event, name=f"GA-{user.username}", price=0)
    Ticket.objects.create(
        event=event,
        tier=tier,
        user=user,
        status=Ticket.TicketStatus.ACTIVE,
        guest_name=user.get_display_name(),
    )


class TestSendScheduledSweep:
    def test_sends_due_absolute_and_skips_future(
        self,
        org: Organization,
        event: Event,
        revel_user_factory: RevelUserFactory,
        django_capture_on_commit_callbacks: t.Any,
    ) -> None:
        owner = org.owner
        _ticket(event, revel_user_factory(username="due_attendee"))
        due = Announcement.objects.create(
            organization=org,
            event=event,
            title="due",
            body="b",
            created_by=owner,
            status=Announcement.AnnouncementStatus.SCHEDULED,
            scheduled_at=timezone.now() - dt.timedelta(minutes=1),
        )
        future = Announcement.objects.create(
            organization=org,
            event=event,
            title="future",
            body="b",
            created_by=owner,
            status=Announcement.AnnouncementStatus.SCHEDULED,
            scheduled_at=timezone.now() + dt.timedelta(hours=1),
        )
        with django_capture_on_commit_callbacks(execute=True):
            result = send_scheduled_announcements()
        due.refresh_from_db()
        future.refresh_from_db()
        assert result["sent"] == 1
        assert due.status == Announcement.AnnouncementStatus.SENT
        assert future.status == Announcement.AnnouncementStatus.SCHEDULED

    def test_relative_due_is_sent(
        self,
        org: Organization,
        revel_user_factory: RevelUserFactory,
        django_capture_on_commit_callbacks: t.Any,
    ) -> None:
        # Event starts in 30 min; offset -60 min => effective send 30 min ago => due.
        ev = Event.objects.create(
            organization=org,
            name="Soon",
            slug="soon",
            event_type=Event.EventType.PUBLIC,
            visibility=Event.Visibility.PUBLIC,
            status=Event.EventStatus.OPEN,
            start=timezone.now() + dt.timedelta(minutes=30),
            end=timezone.now() + dt.timedelta(hours=3),
        )
        _ticket(ev, revel_user_factory(username="rel_attendee"))
        ann = Announcement.objects.create(
            organization=org,
            event=ev,
            title="rel",
            body="b",
            created_by=org.owner,
            status=Announcement.AnnouncementStatus.SCHEDULED,
            schedule_anchor=Announcement.ScheduleAnchor.EVENT_START,
            schedule_offset_minutes=-60,
        )
        with django_capture_on_commit_callbacks(execute=True):
            result = send_scheduled_announcements()
        ann.refresh_from_db()
        assert result["sent"] == 1
        assert ann.status == Announcement.AnnouncementStatus.SENT


class TestResendSweep:
    def test_resends_for_active_event_and_skips_ended(
        self,
        org: Organization,
        event: Event,
        revel_user_factory: RevelUserFactory,
        django_capture_on_commit_callbacks: t.Any,
    ) -> None:
        from events.service import announcement_service

        # Active event: send first (notifies `early`), then a late joiner appears.
        early = revel_user_factory(username="sweep_early")
        _ticket(event, early)
        ann = Announcement.objects.create(
            organization=org,
            event=event,
            title="loc",
            body="b",
            created_by=org.owner,
            resend_to_new_signups=True,
            past_visibility=True,
        )
        with django_capture_on_commit_callbacks(execute=True):
            announcement_service.send_announcement(ann)
        ann.refresh_from_db()
        assert ann.recipient_count == 1

        late = revel_user_factory(username="sweep_late")
        _ticket(event, late)

        # Ended event with resend on -> must be skipped by the sweep.
        ended_ev = Event.objects.create(
            organization=org,
            name="Ended",
            slug="ended",
            event_type=Event.EventType.PUBLIC,
            visibility=Event.Visibility.PUBLIC,
            status=Event.EventStatus.OPEN,
            start=timezone.now() - dt.timedelta(days=2),
            end=timezone.now() - dt.timedelta(days=1),
        )
        _ticket(ended_ev, revel_user_factory(username="ended_attendee"))
        ended_ann = Announcement.objects.create(
            organization=org,
            event=ended_ev,
            title="old",
            body="b",
            created_by=org.owner,
            status=Announcement.AnnouncementStatus.SENT,
            sent_at=timezone.now() - dt.timedelta(days=2),
            resend_to_new_signups=True,
            past_visibility=True,
            recipient_count=1,
        )

        with django_capture_on_commit_callbacks(execute=True):
            result = resend_announcements_to_new_signups()

        assert result["resent"] == 1
        assert result["recipients"] == 1
        ann.refresh_from_db()
        ended_ann.refresh_from_db()
        assert ann.recipient_count == 2
        assert ended_ann.recipient_count == 1  # untouched
