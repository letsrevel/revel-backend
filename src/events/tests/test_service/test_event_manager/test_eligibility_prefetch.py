"""EligibilityService waitlist prefetch."""

import datetime as dt
import uuid

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from conftest import RevelUserFactory
from events.models import Event, OrganizationMember, WaitlistOffer
from events.service.event_manager.service import EligibilityService

pytestmark = pytest.mark.django_db


def test_pending_offer_count_visible_on_event(event: Event, revel_user_factory: RevelUserFactory) -> None:
    other = revel_user_factory()
    WaitlistOffer.objects.create(
        event=event,
        user=other,
        expires_at=timezone.now() + dt.timedelta(hours=1),
        batch_id=uuid.uuid4(),
    )
    viewer = revel_user_factory()
    svc = EligibilityService(viewer, event)
    assert svc.event.pending_waitlist_offer_count == 1
    assert svc.active_waitlist_offer is None


def test_active_waitlist_offer_resolves_for_user(event: Event, revel_user_factory: RevelUserFactory) -> None:
    u = revel_user_factory()
    WaitlistOffer.objects.create(
        event=event,
        user=u,
        expires_at=timezone.now() + dt.timedelta(hours=1),
        batch_id=uuid.uuid4(),
    )
    svc = EligibilityService(u, event)
    assert svc.active_waitlist_offer is not None
    assert svc.active_waitlist_offer.user_id == u.id


def test_expired_offer_does_not_count(event: Event, revel_user_factory: RevelUserFactory) -> None:
    other = revel_user_factory()
    WaitlistOffer.objects.create(
        event=event,
        user=other,
        expires_at=timezone.now() - dt.timedelta(minutes=1),
        batch_id=uuid.uuid4(),
    )
    viewer = revel_user_factory()
    svc = EligibilityService(viewer, event)
    assert svc.event.pending_waitlist_offer_count == 0


@pytest.mark.parametrize("status", [*OrganizationMember.MembershipStatus, None])
def test_member_ids_only_handler_user(
    event: Event, revel_user_factory: RevelUserFactory, status: OrganizationMember.MembershipStatus | None
) -> None:
    """Only the handler user's own membership is loaded; member_ids is {user.id} iff ACTIVE."""
    for _ in range(10):
        OrganizationMember.objects.create(
            organization=event.organization,
            user=revel_user_factory(),
            status=OrganizationMember.MembershipStatus.ACTIVE,
        )
    user = revel_user_factory()
    if status is not None:
        OrganizationMember.objects.create(organization=event.organization, user=user, status=status)
    svc = EligibilityService(user, event)
    assert svc.member_ids == ({user.id} if status == OrganizationMember.MembershipStatus.ACTIVE else set())
    assert len(svc.membership_status_map) <= 1
    assert svc.membership_status_map.get(user.id) == status


def test_eligibility_prefetch_query_count_not_higher(event: Event, revel_user_factory: RevelUserFactory) -> None:
    """Deriving member_ids from the memberships prefetch dropped one query (was 17, now 16)."""
    viewer = revel_user_factory()
    with CaptureQueriesContext(connection) as ctx:
        EligibilityService(viewer, event)
    assert len(ctx) <= 16
