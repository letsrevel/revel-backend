"""EligibilityService waitlist prefetch."""

import datetime as dt
import uuid

import pytest
from django.utils import timezone

from conftest import RevelUserFactory
from events.models import Event, WaitlistOffer
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
