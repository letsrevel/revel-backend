"""Tests for announcement scheduling/unscheduling and resend service functions."""

import datetime as dt

import pytest
from django.utils import timezone

from accounts.models import RevelUser
from conftest import RevelUserFactory
from events.models import Announcement, Event, Organization
from events.service import announcement_service

pytestmark = pytest.mark.django_db


@pytest.fixture
def org_owner(revel_user_factory: RevelUserFactory) -> RevelUser:
    return revel_user_factory(username="sched_owner")


@pytest.fixture
def org(org_owner: RevelUser) -> Organization:
    return Organization.objects.create(name="Sched Org", slug="sched-org", owner=org_owner)


@pytest.fixture
def event(org: Organization) -> Event:
    return Event.objects.create(
        organization=org, name="Sched Event", slug="sched-event",
        event_type=Event.EventType.PUBLIC, visibility=Event.Visibility.PUBLIC,
        status=Event.EventStatus.OPEN,
        start=timezone.now() + dt.timedelta(days=7),
        end=timezone.now() + dt.timedelta(days=7, hours=4),
    )


@pytest.fixture
def draft(org: Organization, org_owner: RevelUser, event: Event) -> Announcement:
    return Announcement.objects.create(
        organization=org, event=event, title="D", body="B",
        created_by=org_owner, status=Announcement.AnnouncementStatus.DRAFT,
    )


class TestScheduleAnnouncement:
    def test_absolute_schedule(self, draft: Announcement) -> None:
        when = timezone.now() + dt.timedelta(hours=2)
        announcement_service.schedule_announcement(draft, scheduled_at=when)
        draft.refresh_from_db()
        assert draft.status == Announcement.AnnouncementStatus.SCHEDULED
        assert draft.scheduled_at == when

    def test_relative_schedule_leaves_scheduled_at_null(self, draft: Announcement) -> None:
        announcement_service.schedule_announcement(
            draft, schedule_anchor=Announcement.ScheduleAnchor.EVENT_START, schedule_offset_minutes=-1440,
        )
        draft.refresh_from_db()
        assert draft.status == Announcement.AnnouncementStatus.SCHEDULED
        assert draft.scheduled_at is None
        assert draft.effective_send_at == draft.event.start - dt.timedelta(minutes=1440)

    def test_past_time_rejected(self, draft: Announcement) -> None:
        with pytest.raises(ValueError):
            announcement_service.schedule_announcement(
                draft, scheduled_at=timezone.now() - dt.timedelta(hours=1),
            )

    def test_unresolvable_relative_rejected(self, org: Organization, org_owner: RevelUser) -> None:
        # No event -> relative cannot resolve.
        ann = Announcement.objects.create(
            organization=org, title="D", body="B", target_all_members=True,
            created_by=org_owner, status=Announcement.AnnouncementStatus.DRAFT,
        )
        with pytest.raises(ValueError):
            announcement_service.schedule_announcement(
                ann, schedule_anchor=Announcement.ScheduleAnchor.EVENT_START, schedule_offset_minutes=-60,
            )

    def test_non_draft_rejected(self, draft: Announcement) -> None:
        draft.status = Announcement.AnnouncementStatus.SENT
        draft.save(update_fields=["status"])
        with pytest.raises(ValueError):
            announcement_service.schedule_announcement(draft, scheduled_at=timezone.now() + dt.timedelta(days=1))

    def test_partial_relative_rejected(self, draft: Announcement) -> None:
        with pytest.raises(ValueError):
            announcement_service.schedule_announcement(
                draft, schedule_anchor=Announcement.ScheduleAnchor.EVENT_START,
            )


class TestUnscheduleAnnouncement:
    def test_unschedule_resets_to_draft(self, draft: Announcement) -> None:
        announcement_service.schedule_announcement(
            draft, schedule_anchor=Announcement.ScheduleAnchor.EVENT_END, schedule_offset_minutes=60,
        )
        announcement_service.unschedule_announcement(draft)
        draft.refresh_from_db()
        assert draft.status == Announcement.AnnouncementStatus.DRAFT
        assert draft.scheduled_at is None
        assert draft.schedule_anchor is None
        assert draft.schedule_offset_minutes is None

    def test_unschedule_non_scheduled_rejected(self, draft: Announcement) -> None:
        with pytest.raises(ValueError):
            announcement_service.unschedule_announcement(draft)
