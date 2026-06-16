"""Tests for announcement scheduling/unscheduling and resend service functions."""

import datetime as dt

import pytest
from django.utils import timezone

from accounts.models import RevelUser
from conftest import RevelUserFactory
from events.models import Announcement, Event, EventRSVP, Organization, Ticket, TicketTier
from events.schema.announcement import (
    AnnouncementCreateSchema,
    AnnouncementScheduleSchema,
    AnnouncementUpdateSchema,
)
from events.service import announcement_service
from notifications.enums import NotificationType
from notifications.models import Notification

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
        organization=org,
        name="Sched Event",
        slug="sched-event",
        event_type=Event.EventType.PUBLIC,
        visibility=Event.Visibility.PUBLIC,
        status=Event.EventStatus.OPEN,
        start=timezone.now() + dt.timedelta(days=7),
        end=timezone.now() + dt.timedelta(days=7, hours=4),
    )


@pytest.fixture
def draft(org: Organization, org_owner: RevelUser, event: Event) -> Announcement:
    return Announcement.objects.create(
        organization=org,
        event=event,
        title="D",
        body="B",
        created_by=org_owner,
        status=Announcement.AnnouncementStatus.DRAFT,
    )


class TestScheduleAnnouncement:
    def test_absolute_schedule(self, draft: Announcement) -> None:
        when = timezone.now() + dt.timedelta(hours=2)
        announcement_service.schedule_announcement(draft, scheduled_at=when)
        draft.refresh_from_db()
        assert draft.status == Announcement.AnnouncementStatus.SCHEDULED
        assert draft.scheduled_at == when

    def test_relative_schedule_leaves_scheduled_at_null(self, draft: Announcement, event: Event) -> None:
        announcement_service.schedule_announcement(
            draft,
            schedule_anchor=Announcement.ScheduleAnchor.EVENT_START,
            schedule_offset_minutes=-1440,
        )
        draft.refresh_from_db()
        assert draft.status == Announcement.AnnouncementStatus.SCHEDULED
        assert draft.scheduled_at is None
        assert draft.effective_send_at == event.start - dt.timedelta(minutes=1440)

    def test_past_time_rejected(self, draft: Announcement) -> None:
        with pytest.raises(ValueError):
            announcement_service.schedule_announcement(
                draft,
                scheduled_at=timezone.now() - dt.timedelta(hours=1),
            )

    def test_unresolvable_relative_rejected(self, org: Organization, org_owner: RevelUser) -> None:
        # No event -> relative cannot resolve.
        ann = Announcement.objects.create(
            organization=org,
            title="D",
            body="B",
            target_all_members=True,
            created_by=org_owner,
            status=Announcement.AnnouncementStatus.DRAFT,
        )
        with pytest.raises(ValueError):
            announcement_service.schedule_announcement(
                ann,
                schedule_anchor=Announcement.ScheduleAnchor.EVENT_START,
                schedule_offset_minutes=-60,
            )

    def test_non_draft_rejected(self, draft: Announcement) -> None:
        draft.status = Announcement.AnnouncementStatus.SENT
        draft.save(update_fields=["status"])
        with pytest.raises(ValueError):
            announcement_service.schedule_announcement(draft, scheduled_at=timezone.now() + dt.timedelta(days=1))

    def test_partial_relative_rejected(self, draft: Announcement) -> None:
        with pytest.raises(ValueError):
            announcement_service.schedule_announcement(
                draft,
                schedule_anchor=Announcement.ScheduleAnchor.EVENT_START,
            )


class TestUnscheduleAnnouncement:
    def test_unschedule_resets_to_draft(self, draft: Announcement) -> None:
        announcement_service.schedule_announcement(
            draft,
            schedule_anchor=Announcement.ScheduleAnchor.EVENT_END,
            schedule_offset_minutes=60,
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


class TestUpdateScheduledGuards:
    def test_clearing_event_on_relative_schedule_raises_valueerror(
        self, org: Organization, org_owner: RevelUser, event: Event
    ) -> None:
        ann = Announcement.objects.create(
            organization=org,
            event=event,
            title="d",
            body="b",
            created_by=org_owner,
            status=Announcement.AnnouncementStatus.DRAFT,
        )
        announcement_service.schedule_announcement(
            ann,
            schedule_anchor=Announcement.ScheduleAnchor.EVENT_START,
            schedule_offset_minutes=-1440,
        )
        with pytest.raises(ValueError):
            announcement_service.update_announcement(
                ann,
                AnnouncementUpdateSchema(event_id=None, target_all_members=True),
            )


class TestResendToNewRecipients:
    def _ticket(self, event: Event, user: RevelUser) -> Ticket:
        tier = TicketTier.objects.create(event=event, name=f"GA-{user.username}", price=0)
        return Ticket.objects.create(
            event=event,
            tier=tier,
            user=user,
            status=Ticket.TicketStatus.ACTIVE,
            guest_name=user.get_display_name(),
        )

    def test_only_new_signups_notified(
        self, org: Organization, org_owner: RevelUser, event: Event, revel_user_factory: RevelUserFactory
    ) -> None:
        early = revel_user_factory(username="early")
        self._ticket(event, early)
        ann = Announcement.objects.create(
            organization=org,
            event=event,
            title="loc",
            body="here",
            created_by=org_owner,
            resend_to_new_signups=True,
            past_visibility=True,
        )
        announcement_service.send_announcement(ann)  # notifies `early`
        ann.refresh_from_db()
        assert ann.recipient_count == 1

        late = revel_user_factory(username="late")
        self._ticket(event, late)

        sent = announcement_service.resend_to_new_recipients(ann)
        assert sent == 1
        assert Notification.objects.filter(
            user=late,
            notification_type=NotificationType.ORG_ANNOUNCEMENT,
            context__announcement_id=str(ann.id),
        ).exists()
        assert (
            Notification.objects.filter(
                user=early,
                notification_type=NotificationType.ORG_ANNOUNCEMENT,
                context__announcement_id=str(ann.id),
            ).count()
            == 1
        )
        ann.refresh_from_db()
        assert ann.recipient_count == 2

    def test_no_new_signups_is_noop(
        self, org: Organization, org_owner: RevelUser, event: Event, revel_user_factory: RevelUserFactory
    ) -> None:
        u = revel_user_factory(username="only")
        EventRSVP.objects.create(event=event, user=u, status=EventRSVP.RsvpStatus.YES)
        ann = Announcement.objects.create(
            organization=org,
            event=event,
            title="x",
            body="y",
            created_by=org_owner,
            resend_to_new_signups=True,
            past_visibility=True,
        )
        announcement_service.send_announcement(ann)
        assert announcement_service.resend_to_new_recipients(ann) == 0

    def test_ended_event_resend_is_noop(
        self, org: Organization, org_owner: RevelUser, revel_user_factory: RevelUserFactory
    ) -> None:
        ended = Event.objects.create(
            organization=org,
            name="Ended",
            slug="ended-resend",
            event_type=Event.EventType.PUBLIC,
            visibility=Event.Visibility.PUBLIC,
            status=Event.EventStatus.OPEN,
            start=timezone.now() - dt.timedelta(days=2),
            end=timezone.now() - dt.timedelta(days=1),
        )
        self._ticket(ended, revel_user_factory(username="post_end_joiner"))
        ann = Announcement.objects.create(
            organization=org,
            event=ended,
            title="x",
            body="y",
            created_by=org_owner,
            status=Announcement.AnnouncementStatus.SENT,
            sent_at=timezone.now() - dt.timedelta(days=2),
            resend_to_new_signups=True,
            past_visibility=True,
        )
        assert announcement_service.resend_to_new_recipients(ann) == 0

    def test_requires_sent_and_flag(self, draft: Announcement) -> None:
        with pytest.raises(ValueError):
            announcement_service.resend_to_new_recipients(draft)

    def test_sent_but_flag_off_raises(self, org: Organization, org_owner: RevelUser, event: Event) -> None:
        ann = Announcement.objects.create(
            organization=org,
            event=event,
            title="x",
            body="y",
            created_by=org_owner,
            status=Announcement.AnnouncementStatus.SENT,
            sent_at=timezone.now(),
            resend_to_new_signups=False,
        )
        with pytest.raises(ValueError):
            announcement_service.resend_to_new_recipients(ann)


class TestScheduleSchema:
    def test_absolute_ok(self) -> None:
        s = AnnouncementScheduleSchema(scheduled_at=timezone.now() + dt.timedelta(days=1))
        assert s.schedule_anchor is None

    def test_relative_ok(self) -> None:
        s = AnnouncementScheduleSchema(
            schedule_anchor=Announcement.ScheduleAnchor.EVENT_START,
            schedule_offset_minutes=-1440,
        )
        assert s.scheduled_at is None

    def test_both_rejected(self) -> None:
        with pytest.raises(ValueError):
            AnnouncementScheduleSchema(
                scheduled_at=timezone.now() + dt.timedelta(days=1),
                schedule_anchor=Announcement.ScheduleAnchor.EVENT_START,
                schedule_offset_minutes=-60,
            )

    def test_neither_rejected(self) -> None:
        with pytest.raises(ValueError):
            AnnouncementScheduleSchema()

    def test_partial_relative_rejected(self) -> None:
        with pytest.raises(ValueError):
            AnnouncementScheduleSchema(schedule_anchor=Announcement.ScheduleAnchor.EVENT_START)


class TestCreateSchemaResend:
    def test_resend_forces_past_visibility(self) -> None:
        from uuid import uuid4

        s = AnnouncementCreateSchema(
            title="t",
            body="b",
            event_id=uuid4(),
            resend_to_new_signups=True,
            past_visibility=False,
        )
        assert s.past_visibility is True

    def test_resend_requires_event(self) -> None:
        with pytest.raises(ValueError):
            AnnouncementCreateSchema(title="t", body="b", target_all_members=True, resend_to_new_signups=True)
